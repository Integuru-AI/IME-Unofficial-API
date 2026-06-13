import re
import json
import uuid
from urllib.parse import urlencode, parse_qs, urlparse, quote
from html.parser import HTMLParser
from curl_cffi import requests as curl_requests

# Runtime-injected globals
try:
    _BASE = BASE_URL
except NameError:
    _BASE = "https://apps.trustedhomeservices.com"

try:
    _APP = APP_URL
except NameError:
    _APP = _BASE

CATEGORY_MAP = {
    "Electric Tank Water Heater": "1411",
    "Electric Tankless Water Heater": "1412",
    "Gas Tank Water Heater": "1408",
    "Gas Tankless Water Heater": "1409",
    "Solar Water Heater": "1803",
}

LOCATION_MAP = {
    "1st Floor": "59",
    "2nd Floor": "60",
    "3rd Floor": "61",
    "Attic": "62",
    "Basement": "57",
    "Crawl Space": "58",
    "Customers Home": "300",
    "Customer's Home": "300",
    "Garage": "56",
    "Other": "274",
}


def run(headers, user_input):
    """Create an estimate on a work order."""

    # --- Validate required inputs ---
    work_order_number = user_input.get("work_order_number")
    if not work_order_number:
        return {"status_code": 400, "body": {"error": "work_order_number is required"}}

    category_name = user_input.get("category", "")
    if not category_name or category_name not in CATEGORY_MAP:
        return {"status_code": 400, "body": {"error": f"category is required. Valid options: {list(CATEGORY_MAP.keys())}"}}

    location_name = user_input.get("location", "")
    location_id = LOCATION_MAP.get(location_name)
    if not location_id:
        return {"status_code": 400, "body": {"error": f"location is required. Valid options: {list(LOCATION_MAP.keys())}"}}

    collection_name = user_input.get("collection", "")
    if not collection_name:
        return {"status_code": 400, "body": {"error": "collection is required (e.g. '50-59 Gallons')"}}

    product_name = user_input.get("product", "")
    if not product_name:
        return {"status_code": 400, "body": {"error": "product is required (e.g. '50-GAL 6YR ELEC TALL WH' or 'Other')"}}

    water_heater_number = user_input.get("water_heater_number", "")
    water_heater_price = user_input.get("water_heater_price")
    materials_price = user_input.get("materials_price", 0)
    labor_price = user_input.get("labor_price", 0)
    quantity = user_input.get("quantity", 1)
    estimator_name = user_input.get("estimator_name", "")

    category_id = CATEGORY_MAP[category_name]

    try:
        result = _create_estimate(
            headers, work_order_number, category_name, category_id,
            location_name, location_id, collection_name, product_name,
            water_heater_number, water_heater_price,
            materials_price, labor_price, quantity, estimator_name,
        )
        return result
    except Exception as e:
        return {"status_code": 500, "body": {"error": str(e)}}


# === PRIVATE ===


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

class _InputParser(HTMLParser):
    """Extract <input> and <select><option> data from HTML fragments."""

    def __init__(self):
        super().__init__()
        self.inputs = []          # list of (name, value)
        self.selects = {}         # name -> [(value, text, selected)]
        self._cur_select = None
        self._cur_opt_val = None
        self._cur_opt_sel = False
        self._cur_opt_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input":
            name = a.get("name", "")
            value = a.get("value", "")
            if name:
                self.inputs.append((name, value))
        elif tag == "select":
            name = a.get("name", "")
            if name:
                self._cur_select = name
                self.selects.setdefault(name, [])
        elif tag == "option" and self._cur_select:
            self._cur_opt_val = a.get("value", "")
            self._cur_opt_sel = "selected" in a
            self._cur_opt_text = ""

    def handle_data(self, data):
        if self._cur_select and self._cur_opt_val is not None:
            self._cur_opt_text += data

    def handle_endtag(self, tag):
        if tag == "option" and self._cur_select:
            self.selects[self._cur_select].append(
                (self._cur_opt_val, self._cur_opt_text.strip(), self._cur_opt_sel)
            )
            self._cur_opt_val = None
            self._cur_opt_sel = False
            self._cur_opt_text = ""
        elif tag == "select":
            self._cur_select = None


def _parse_html(html):
    p = _InputParser()
    p.feed(html)
    return p


def _extract_token(html):
    m = re.search(
        r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"', html
    )
    if not m:
        m = re.search(r'__RequestVerificationToken[^v]*value="([^"]+)"', html)
    return m.group(1) if m else None


def _find_option(options, user_text):
    """Fuzzy-match user_text against a list of (value, text, selected) options."""
    user_lower = user_text.strip().lower()
    # exact match first
    for val, text, _ in options:
        if text.strip().lower() == user_lower:
            return val, text
    # substring match
    for val, text, _ in options:
        if user_lower in text.strip().lower():
            return val, text
    return None, None


def _hidden_dict(parser):
    """Build a dict of name->value from parsed hidden inputs (last wins)."""
    d = {}
    for name, val in parser.inputs:
        d[name] = val
    return d


def _session(headers):
    """Build a curl_cffi Session with auth cookies."""
    s = curl_requests.Session(impersonate="chrome131")
    cookie_str = headers.get("Cookie", "")
    if cookie_str:
        for part in cookie_str.split("; "):
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain="apps.trustedhomeservices.com")
    return s


def _is_login_page(resp):
    if resp.url and "/account/login" in resp.url.lower():
        return True
    if "login" in resp.text[:500].lower() and "__RequestVerificationToken" not in resp.text[:2000]:
        return True
    return False


def _create_estimate(
    headers, work_order_number, category_name, category_id,
    location_name, location_id, collection_name, product_name,
    water_heater_number, water_heater_price,
    materials_price, labor_price, quantity, estimator_name,
):
    """Execute the full multi-step estimate creation workflow."""
    base = _BASE.rstrip("/")

    common_headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    s = _session(headers)

    # -----------------------------------------------------------------------
    # Step 1: Resolve MIC work order number -> internal work order ID
    # -----------------------------------------------------------------------
    import_url = f"{base}/WorkOrderNew/Import?woID={work_order_number}"
    resp = s.get(import_url, allow_redirects=True, timeout=30)
    if _is_login_page(resp):
        return {"status_code": 401, "body": {"error": "Session expired"}}

    # The redirect lands on /WorkOrderNew/Details/{internal_id}?programId=...
    final_url = str(resp.url)
    m = re.search(r"/WorkOrderNew/Details/(\d+)", final_url)
    if not m:
        # Try parsing from the page HTML as fallback
        m = re.search(r'id="workOrderId_MIC"\s+value="' + str(work_order_number) + '"', resp.text)
        if not m:
            return {"status_code": 404, "body": {"error": f"Work order {work_order_number} not found"}}
        # Try to find internal ID from page
        m2 = re.search(r'/WorkOrderNew/Details/(\d+)', resp.text)
        if not m2:
            return {"status_code": 500, "body": {"error": "Could not resolve internal work order ID"}}
        internal_wo_id = m2.group(1)
    else:
        internal_wo_id = m.group(1)

    # Extract programId from URL if present
    parsed_url = urlparse(final_url)
    program_id_match = re.search(r'programId=(\d+)', final_url)
    program_id = program_id_match.group(1) if program_id_match else ""

    # Resolve estimator name -> ID from the WO Details page EstimatorAcctId dropdown
    estimator_id = "0"
    if estimator_name:
        wo_html = resp.text
        est_select_match = re.search(r'id="EstimatorAcctId".*?</select>', wo_html, re.DOTALL)
        if est_select_match:
            est_options = re.findall(
                r'<option[^>]*value="(\d+)"[^>]*>(.*?)</option>',
                est_select_match.group(0),
            )
            # Try exact match first, then substring match on name
            est_lower = estimator_name.strip().lower()
            for val, text in est_options:
                if text.strip().lower() == est_lower:
                    estimator_id = val
                    break
            else:
                for val, text in est_options:
                    # Match on last name or partial name (format: "Last, First (username)")
                    if est_lower in text.strip().lower():
                        estimator_id = val
                        break
        if estimator_id == "0":
            return {"status_code": 400, "body": {"error": f"Estimator '{estimator_name}' not found in work order dropdown"}}

        # Update the estimator on the work order via /WorkOrderNew/UpdateEstimator
        # This must be done before creating the estimate (the Create form reads from the WO)
        update_est_url = f"{base}/WorkOrderNew/UpdateEstimator"
        update_est_resp = s.post(
            update_est_url,
            data=f"estimatorId={estimator_id}&workOrderNumber={work_order_number}",
            headers=common_headers,
            timeout=30,
        )
        if update_est_resp.status_code != 200:
            return {"status_code": 500, "body": {"error": f"Failed to update estimator: HTTP {update_est_resp.status_code}"}}

    # -----------------------------------------------------------------------
    # Step 2: Load the Create estimate form
    # -----------------------------------------------------------------------
    return_url = f"/WorkOrderNew/Details/{internal_wo_id}"
    create_url = f"{base}/GenericEnhancementProjectRoom/Create?returnUrl={quote(return_url, safe='')}&workOrderId={internal_wo_id}"
    resp = s.get(create_url, timeout=30)

    if _is_login_page(resp):
        return {"status_code": 401, "body": {"error": "Session expired"}}

    create_html = resp.text

    # Extract CSRF token
    csrf_token = _extract_token(create_html)
    if not csrf_token:
        return {"status_code": 500, "body": {"error": "Could not extract CSRF token from form"}}

    # Parse form fields
    form_parser = _parse_html(create_html)
    form_fields = _hidden_dict(form_parser)

    # Extract work order details from hidden fields
    wo_zip = form_fields.get("WorkOrderDetails.ZipCode", "")
    wo_src_program = form_fields.get("WorkOrderDetails.SrcProgramId", "")
    wo_order_date = form_fields.get("WorkOrderDetails.OrderDate", "")
    wo_mic_id = form_fields.get("WorkOrderDetails.MIC_WorkOrderId", work_order_number)
    wo_customer_name = form_fields.get("WorkOrderDetails.CustomerName", "")

    # Auto-generate project name: "{Last Name} Estimate"
    # Customer name comes as "Smith, Leonard" -- extract the last name
    if wo_customer_name and "," in wo_customer_name:
        last_name = wo_customer_name.split(",")[0].strip()
    elif wo_customer_name:
        last_name = wo_customer_name.split()[0].strip()
    else:
        last_name = "Customer"
    project_name = f"{last_name} Estimate"

    wo_contact_id = form_fields.get("WorkOrderDetails.ContactId", "")
    wo_affiliate_id = form_fields.get("WorkOrderDetails.AffiliateId", "")
    wo_source_id = form_fields.get("WorkOrderDetails.SourceId", "")

    # Get available category IDs
    avail_categories = form_fields.get("Location_0_AvailableCategoryIdsCsvString", "1411,1412,1408,1409,1803")

    # -----------------------------------------------------------------------
    # Step 3: POST RefreshForm -- set category + location, get collection/product
    # -----------------------------------------------------------------------
    location_guid = str(uuid.uuid4())

    refresh_form_data = {
        "__RequestVerificationToken": csrf_token,
        "ReturnUrl": return_url,
        "Specification.WorkOrderId": internal_wo_id,
        "Specification.ProjectId": "0",
        "Specification.EstimatorAccountId": estimator_id,
        "WorkOrderDetails.ZipCode": wo_zip,
        "WorkOrderDetails.SrcProgramId": wo_src_program,
        "WorkOrderDetails.OrderDate": wo_order_date,
        "WorkOrderDetails.MIC_WorkOrderId": wo_mic_id,
        "WorkOrderDetails.CustomerName": wo_customer_name,
        "WorkOrderDetails.ContactId": wo_contact_id,
        "WorkOrderDetails.AffiliateId": wo_affiliate_id,
        "Project.NewlyCreatedAndNotSaved": "False",
        "Project.CanSaveTemplate": "False",
        "Project.CanUseTemplate": "False",
        "isLocationMapping": "False",
        "ProjectId": "0",
        "Project.Id": "0",
        "Project.WorkOrderId": internal_wo_id,
        "Project.TemplateId": "",
        "Project.DiagramId": "",
        "Project.Name": project_name,
        "callbackSection": "sectionJobLevelLabor",
        "workOrderId": internal_wo_id,
        "Project.EstimateSentDate": "",
        "Project.CreatedDate": "",
        "Project.JobLevelLaborItems[0].SelectedItem": "",
        "Project.JobLevelLaborItems[0].UoM": "Price",
        "Project.JobLevelLaborItems[0].ReadonlyQuantity": "False",
        "Project.JobLevelLaborItems[0].Quantity": "0",
        "Project.JobLevelLaborItems[0].Quantitydollar": "$0.00",
        "Project.JobLevelLaborItems[0].CategoryRank": "1000",
        "Project.JobLevelLaborItems[0].MaterialType": "Labor",
        "Project.JobLevelLaborItems[1].SelectedItem": "",
        "Project.JobLevelLaborItems[1].UoM": "Price",
        "Project.JobLevelLaborItems[1].ReadonlyQuantity": "False",
        "Project.JobLevelLaborItems[1].Quantity": "0",
        "Project.JobLevelLaborItems[1].Quantitydollar": "$0.00",
        "Project.JobLevelLaborItems[1].CategoryRank": "1000",
        "Project.JobLevelLaborItems[1].MaterialType": "Labor",
        "Project.MinEstimateQty": "",
        "TemplateName": "",
        "action": "",
        "newProjectId": "",
        "newRoomId": "",
        "Specification.Locations[0].LocationGuid": location_guid,
        "Specification.Locations[0].ParentGuid": "00000000-0000-0000-0000-000000000000",
        "Specification.Locations[0].TemplateRoomId": "",
        "Specification.Locations[0].Id": "0",
        "Specification.Locations[0].IncludeInEstimate": "true",
        "Specification.Locations[0].LocationAndDescription": location_name,
        "Specification.Locations[0].LocationTypeId": location_id,
        "Specification.Locations[0].LocationName": "",
        "Specification.Locations[0].CloudShapeId": "",
        "Specification.Locations[0].CloudArea": "",
        "Specification.Locations[0].CloudPerimeter": "",
        "Specification.Locations[0].CloudRoomName": "",
        "Specification.Locations[0].CloudWidth": "",
        "Specification.Locations[0].CloudTread": "",
        "Specification.Locations[0].CloudSteps": "",
        "Specification.Locations[0].CloudRiser": "",
        "Specification.Locations[0].Specs2[0].ProductCategoryId": category_id,
        "Specification.Locations[0].Specs2[0].ApplyManualWasteFromSpecSheet": "true",
        "Specification.Locations[0].Specs2[0].ManualWasteQty": "",
        "Specification.Locations[0].Specs2[0].AllowManualWasteAdjustment": "False",
        "Specification.Locations[0].Specs2[0].Use3rdPartyMeasurementWaste": "False",
        "Specification.Locations[0].Specs2[0].DefaultManualWastePercent": "",
        "Specification.Locations[0].Specs2[0].MeasurementTypeId": "",
        "Specification.Locations[0].Specs2[0].UoM": "",
        "Specification.Locations[0].Specs2[0].QuantityDisabled": "False",
        "Specification.Locations[0].Specs2[0].UnitsOfMeasure": "System.Collections.Generic.List`1[System.Web.Mvc.SelectListItem]",
        "Specification.Locations[0].Specs2[0].EnableRangeVariables": "False",
        "Specification.Locations[0].Specs2[0].IsAdditionalProductQtyUsed": "False",
        "Specification.Locations[0].Specs2[0].CustomDynamicAssortments[0].MaterialType": "Grade",
        "Specification.Locations[0].Specs2[0].CustomDynamicAssortments[1].MaterialType": "Subfloor",
        "Specification.Locations[0].Specs2[0].ProductCollectionId": "",
        "Specification.Locations[0].Specs2[0].ProductId": "",
        "Specification.Locations[0].Specs2[0].PreviousSelectedProductId": "",
        "Specification.Locations[0].Specs2[0].PreviousSelectedUomId": "",
        "Specification.Locations[0].Specs2[0].Quantity": "",
        "Specification.Locations[0].Specs2[0].ProductSelectionForced": "False",
        "Specification.Locations[0].Comments": "",
        "Project.IsAffiliatesOwnSpecSheet": "False",
        "locationIndex": "0",
        "forceProductSelection": "false",
        "actionType": "null",
        "projectRoomId": "undefined",
    }

    refresh_url = f"{base}/GenericEnhancementProjectRoom/RefreshForm"
    resp = s.post(
        refresh_url,
        data=refresh_form_data,
        headers=common_headers,
        timeout=30,
    )

    if _is_login_page(resp):
        return {"status_code": 401, "body": {"error": "Session expired"}}

    refresh_html = resp.text
    refresh_parser = _parse_html(refresh_html)

    # Extract new CSRF token from refreshed form if present
    new_token = _extract_token(refresh_html)
    if new_token:
        csrf_token = new_token

    # Resolve collection name -> ID
    collection_select_name = "Specification.Locations[0].Specs2[0].ProductCollectionId"
    collection_options = refresh_parser.selects.get(collection_select_name, [])
    collection_id, collection_matched = _find_option(collection_options, collection_name)
    if not collection_id:
        avail = [t for _, t, _ in collection_options if t and t != "Select item..."]
        return {"status_code": 400, "body": {"error": f"Collection '{collection_name}' not found. Available: {avail}"}}

    # Resolve product name -> ID
    product_select_name = "Specification.Locations[0].Specs2[0].ProductId"
    product_options = refresh_parser.selects.get(product_select_name, [])
    product_id, product_matched = _find_option(product_options, product_name)
    if not product_id:
        avail = [t for _, t, _ in product_options if t and t not in ("Select item...", "")]
        return {"status_code": 400, "body": {"error": f"Product '{product_name}' not found. Available (first 20): {avail[:20]}"}}

    # Parse hidden fields from refresh response for form state
    refresh_fields = _hidden_dict(refresh_parser)

    # -----------------------------------------------------------------------
    # Step 4a: POST UpdateSection -- set collection (no product yet)
    # The browser triggers UpdateSection when the collection dropdown changes,
    # before the product is selected. The server needs this intermediate call.
    # -----------------------------------------------------------------------
    update_url = f"{base}/GenericEnhancementProjectRoom/UpdateSection"

    base_update_data = {
        "WorkOrderDetails.SourceId": wo_source_id or refresh_fields.get("WorkOrderDetails.SourceId", ""),
        "CanUseMeasureDiagram": "False",
        "Specification.DiagramPlatformId": "",
        "callbackSection": "specs2",
        "locationIndexValue": "0",
        "Specification.Locations[0].Specs2[0].ProductCategoryId": category_id,
        "Location_0_AvailableCategoryIdsCsvString": avail_categories,
        "IsRemeasure": "False",
        "Specification.Locations[0].LocationName": refresh_fields.get("Specification.Locations[0].LocationName", location_name),
        "Specification.Locations[0].CloudShapeId": "",
        "Specification.Locations[0].CloudArea": "",
        "Specification.Locations[0].CloudPerimeter": "",
        "Specification.Locations[0].CloudRoomName": "",
        "Specification.Locations[0].CloudWidth": "",
        "Specification.Locations[0].CloudTread": "",
        "Specification.Locations[0].CloudSteps": "",
        "Specification.Locations[0].CloudRiser": "",
        "Specification.Locations[0].LocationTypeId": location_id,
        "Specification.Locations[0].Specs2[0].ApplyManualWasteFromSpecSheet": refresh_fields.get("Specification.Locations[0].Specs2[0].ApplyManualWasteFromSpecSheet", "true"),
        "Specification.Locations[0].Specs2[0].ManualWasteQty": "",
        "Specification.Locations[0].Specs2[0].AllowManualWasteAdjustment": "False",
        "Specification.Locations[0].Specs2[0].Use3rdPartyMeasurementWaste": "False",
        "Specification.Locations[0].Specs2[0].DefaultManualWastePercent": "",
        "Specification.Locations[0].Specs2[0].MeasurementTypeId": "",
        "Specification.Locations[0].Specs2[0].UoM": refresh_fields.get("Specification.Locations[0].Specs2[0].UoM", ""),
        "Specification.Locations[0].Specs2[0].QuantityDisabled": "False",
        "Specification.Locations[0].Specs2[0].UnitsOfMeasure": "System.Collections.Generic.List`1[System.Web.Mvc.SelectListItem]",
        "Specification.Locations[0].Specs2[0].EnableRangeVariables": "False",
        "Specification.Locations[0].Specs2[0].IsAdditionalProductQtyUsed": "False",
        "Specification.Locations[0].Specs2[0].CustomDynamicAssortments[0].MaterialType": "Grade",
        "Specification.Locations[0].Specs2[0].CustomDynamicAssortments[0].SelectedItem": "",
        "Specification.Locations[0].Specs2[0].CustomDynamicAssortments[1].MaterialType": "Subfloor",
        "Specification.Locations[0].Specs2[0].CustomDynamicAssortments[1].SelectedItem": "",
        "Specification.Locations[0].Specs2[0].ProductSelectionForced": "False",
        # Location summary fields (from #locationSummary section)
        "Specification.ProjectId": "0",
        "Specification.WorkOrderId": internal_wo_id,
        "Specification.Locations[0].LocationGuid": location_guid,
        "Specification.Locations[0].ParentGuid": "00000000-0000-0000-0000-000000000000",
        "Specification.Locations[0].TemplateRoomId": "",
        "Specification.Locations[0].Id": "0",
        "Specification.Locations[0].IncludeInEstimate": "true",
        "Specification.Locations[0].LocationAndDescription": location_name,
    }

    # Collection-only call (no product selected yet)
    collection_update_data = {
        **base_update_data,
        "Specification.Locations[0].Specs2[0].ProductCollectionId": collection_id,
        "Specification.Locations[0].Specs2[0].ProductId": "",
        "Specification.Locations[0].Specs2[0].PreviousSelectedProductId": "",
        "Specification.Locations[0].Specs2[0].PreviousSelectedUomId": "",
        "Specification.Locations[0].Specs2[0].Quantity": "",
    }

    resp = s.post(update_url, data=collection_update_data, headers=common_headers, timeout=30)
    if _is_login_page(resp):
        return {"status_code": 401, "body": {"error": "Session expired"}}

    # Parse 4a response to build 4b data from the updated HTML (mimics browser DOM replacement)
    resp_4a_html = resp.text
    parser_4a = _parse_html(resp_4a_html)
    fields_4a = _hidden_dict(parser_4a)

    # Re-resolve product from the 4a response (product list may have changed after collection update)
    product_options_4a = parser_4a.selects.get(product_select_name, [])
    if product_options_4a:
        product_id_4a, product_matched_4a = _find_option(product_options_4a, product_name)
        if product_id_4a:
            product_id = product_id_4a
            product_matched = product_matched_4a

    # -----------------------------------------------------------------------
    # Step 4b: POST UpdateSection -- now select product, get InstallMethodId + DynamicAssortments
    # Build from 4a response fields (mimics browser serializing updated #specs2 HTML)
    # -----------------------------------------------------------------------
    # Start with all hidden fields from the 4a response (#specs2 section)
    product_update_data = dict(fields_4a)
    # Add selected values from 4a selects (browser serializes current select values)
    for sel_name, sel_opts in parser_4a.selects.items():
        for val, text, selected in sel_opts:
            if selected and val:
                product_update_data[sel_name] = val
                break
    # Add #locationSummary fields (not in #specs2 response)
    product_update_data["Specification.ProjectId"] = "0"
    product_update_data["Specification.WorkOrderId"] = internal_wo_id
    product_update_data["Specification.Locations[0].LocationGuid"] = location_guid
    product_update_data["Specification.Locations[0].ParentGuid"] = "00000000-0000-0000-0000-000000000000"
    product_update_data["Specification.Locations[0].TemplateRoomId"] = ""
    product_update_data["Specification.Locations[0].Id"] = "0"
    product_update_data["Specification.Locations[0].IncludeInEstimate"] = "true"
    product_update_data["Specification.Locations[0].LocationAndDescription"] = location_name
    # Override product to the user's selection
    product_update_data["Specification.Locations[0].Specs2[0].ProductId"] = product_id

    resp = s.post(update_url, data=product_update_data, headers=common_headers, timeout=30)
    if _is_login_page(resp):
        return {"status_code": 401, "body": {"error": "Session expired"}}

    update_html = resp.text
    update_parser = _parse_html(update_html)
    update_fields = _hidden_dict(update_parser)

    # Extract InstallMethodId and UoM from the updated section
    # InstallMethodId is a <select> element, so check selects dict first, then hidden fields
    install_method_id = update_fields.get("Specification.Locations[0].Specs2[0].InstallMethodId", "")
    if not install_method_id:
        im_select_name = "Specification.Locations[0].Specs2[0].InstallMethodId"
        im_options = update_parser.selects.get(im_select_name, [])
        for val, text, sel in im_options:
            if sel and val:
                install_method_id = val
                break
        if not install_method_id:
            # Use any non-empty option as fallback
            for val, text, _ in im_options:
                if val:
                    install_method_id = val
                    break
    uom_id = update_fields.get("Specification.Locations[0].Specs2[0].UoM", "10")

    # Extract DynamicAssortment item IDs for materials, labor, protection plan
    # These are populated by the server after product selection
    # We need to find the SelectedItem options for each group

    # Materials group (Sundry / Miscellaneous Materials)
    materials_select = "Specification.Locations[0].Specs2[0].DynamicAssortments[0].SelectedItem"
    materials_options = update_parser.selects.get(materials_select, [])
    materials_item_id = ""
    # Look for "Other" in materials
    for val, text, _ in materials_options:
        if text.strip().lower() == "other":
            materials_item_id = val
            break
    if not materials_item_id and materials_options:
        # Just use first non-empty option
        for val, text, _ in materials_options:
            if val:
                materials_item_id = val
                break

    # Labor group (Water Heater Installation Labor / Accessory)
    labor_select = "Specification.Locations[0].Specs2[0].DynamicAssortments[1].SelectedItem"
    labor_options = update_parser.selects.get(labor_select, [])
    labor_item_id = ""
    for val, text, sel in labor_options:
        if val:
            labor_item_id = val
            break

    # Extended Protection Plan group
    protection_select = "Specification.Locations[0].Specs2[0].DynamicAssortments[2].SelectedItem"
    protection_options = update_parser.selects.get(protection_select, [])
    protection_item_id = ""
    for val, text, sel in protection_options:
        if val:
            protection_item_id = val
            break

    # Extract DynamicAssortment metadata from hidden fields
    materials_group_key = update_fields.get(
        "Specification.Locations[0].Specs2[0].DynamicAssortments[0].GroupKey",
        "C_Miscellaneous Materials_Sundry",
    )
    materials_material_type = update_fields.get(
        "Specification.Locations[0].Specs2[0].DynamicAssortments[0].MaterialType",
        "Sundry",
    )
    labor_group_key = update_fields.get(
        "Specification.Locations[0].Specs2[0].DynamicAssortments[1].GroupKey",
        "C_Water Heater Installation Labor_Accessory",
    )
    labor_material_type = update_fields.get(
        "Specification.Locations[0].Specs2[0].DynamicAssortments[1].MaterialType",
        "Accessory",
    )
    protection_group_key = update_fields.get(
        "Specification.Locations[0].Specs2[0].DynamicAssortments[2].GroupKey",
        "C_Extended Protection Plan_Accessory",
    )
    protection_material_type = update_fields.get(
        "Specification.Locations[0].Specs2[0].DynamicAssortments[2].MaterialType",
        "Accessory",
    )

    # If UpdateSection didn't return DynamicAssortments (no product-level assortments yet),
    # we may need to call RefreshForm again with the product selected.
    # Fall back to known defaults from the captured session.
    if not materials_item_id:
        materials_item_id = "2933"  # "Other"
    if not labor_item_id:
        labor_item_id = "25204"   # "Water Heater Installation - Labor"
    if not protection_item_id:
        protection_item_id = "25183"  # None / default

    # -----------------------------------------------------------------------
    # Step 5: Build and submit the final Create POST
    # -----------------------------------------------------------------------
    # Build form data list (preserves duplicate keys like ASP.NET MVC expects)
    form_pairs = []

    def add(name, value=""):
        form_pairs.append((name, str(value)))

    # CSRF + routing
    add("__RequestVerificationToken", csrf_token)
    add("ReturnUrl", return_url)
    add("Specification.WorkOrderId", internal_wo_id)
    add("Specification.ProjectId", "0")
    add("Specification.EstimatorAccountId", estimator_id)

    # Work order details
    add("WorkOrderDetails.ZipCode", wo_zip)
    add("WorkOrderDetails.SrcProgramId", wo_src_program)
    add("WorkOrderDetails.OrderDate", wo_order_date)
    add("WorkOrderDetails.MIC_WorkOrderId", wo_mic_id)
    add("WorkOrderDetails.CustomerName", wo_customer_name)
    add("ReturnUrl", return_url)

    # Project flags
    add("Project.NewlyCreatedAndNotSaved", "False")
    add("Project.CanSaveTemplate", "False")
    add("Project.CanUseTemplate", "False")
    add("WorkOrderDetails.ContactId", wo_contact_id)
    add("WorkOrderDetails.AffiliateId", wo_affiliate_id)
    add("isLocationMapping", "False")
    add("ProjectId", "0")
    add("ReturnUrl", return_url)

    # Project
    add("Project.Id", "0")
    add("Project.WorkOrderId", internal_wo_id)
    add("Project.TemplateId", "")
    add("Project.DiagramId", "")
    add("Project.Name", project_name)
    add("callbackSection", "sectionJobLevelLabor")
    add("workOrderId", internal_wo_id)
    add("Project.EstimateSentDate", "")
    add("Project.CreatedDate", "")

    # Job Level Labor Items (Permits) - two empty slots
    for i in range(2):
        add(f"Project.JobLevelLaborItems[{i}].SelectedItem", "")
        add(f"Project.JobLevelLaborItems[{i}].UoM", "Price")
        add(f"Project.JobLevelLaborItems[{i}].ReadonlyQuantity", "False")
        add(f"Project.JobLevelLaborItems[{i}].Quantity", "0")
        add(f"Project.JobLevelLaborItems[{i}].Quantitydollar", "$0.00")
        add(f"Project.JobLevelLaborItems[{i}].CategoryRank", "1000")
        add(f"Project.JobLevelLaborItems[{i}].MaterialType", "Labor")

    add("Project.MinEstimateQty", "")
    add("TemplateName", "")
    add("TemplateName", "")
    add("action", "")
    add("newProjectId", "")
    add("newRoomId", "")

    # Specification (duplicate top-level)
    add("Specification.ProjectId", "0")
    add("Specification.WorkOrderId", internal_wo_id)

    # Location
    add("Specification.Locations[0].LocationGuid", location_guid)
    add("Specification.Locations[0].ParentGuid", "00000000-0000-0000-0000-000000000000")
    add("Specification.Locations[0].TemplateRoomId", "")
    add("Specification.Locations[0].Id", "0")
    add("Specification.Locations[0].IncludeInEstimate", "true")
    add("Specification.Locations[0].IncludeInEstimate", "false")
    add("Specification.Locations[0].LocationAndDescription", location_name)
    add("Specification.Locations[0].LocationTypeId", location_id)
    add("callbackSection", "Location_0")

    # Source + diagram
    add("WorkOrderDetails.SourceId", wo_source_id or refresh_fields.get("WorkOrderDetails.SourceId", "33"))
    add("CanUseMeasureDiagram", "False")
    add("Specification.DiagramPlatformId", "")

    # Specs2
    add("callbackSection", "specs2")
    add("locationIndexValue", "0")
    add("Specification.Locations[0].Specs2[0].ProductCategoryId", category_id)
    add("Location_0_AvailableCategoryIdsCsvString", avail_categories)
    add("IsRemeasure", "False")
    add("Specification.Locations[0].LocationName", refresh_fields.get("Specification.Locations[0].LocationName", location_name))

    # Cloud fields (empty)
    for f in ["CloudShapeId", "CloudArea", "CloudPerimeter", "CloudRoomName", "CloudWidth", "CloudTread", "CloudSteps", "CloudRiser"]:
        add(f"Specification.Locations[0].{f}", "")
    add("Specification.Locations[0].LocationTypeId", location_id)

    # Specs hidden fields
    add("Specification.Locations[0].Specs2[0].ApplyManualWasteFromSpecSheet", "true")
    add("Specification.Locations[0].Specs2[0].ManualWasteQty", "")
    add("Specification.Locations[0].Specs2[0].AllowManualWasteAdjustment", "False")
    add("Specification.Locations[0].Specs2[0].Use3rdPartyMeasurementWaste", "False")
    add("Specification.Locations[0].Specs2[0].DefaultManualWastePercent", "")
    add("Specification.Locations[0].Specs2[0].MeasurementTypeId", "")
    add("Specification.Locations[0].Specs2[0].UoM", uom_id)
    add("Specification.Locations[0].Specs2[0].QuantityDisabled", "False")
    add("Specification.Locations[0].Specs2[0].UnitsOfMeasure", "System.Collections.Generic.List`1[System.Web.Mvc.SelectListItem]")
    add("Specification.Locations[0].Specs2[0].EnableRangeVariables", "False")
    add("Specification.Locations[0].Specs2[0].IsAdditionalProductQtyUsed", "False")

    # Custom Dynamic Assortments (Grade, Subfloor - empty)
    add("Specification.Locations[0].Specs2[0].CustomDynamicAssortments[0].MaterialType", "Grade")
    add("Specification.Locations[0].Specs2[0].CustomDynamicAssortments[1].MaterialType", "Subfloor")

    # Product selection
    add("Specification.Locations[0].Specs2[0].ProductCollectionId", collection_id)
    add("Specification.Locations[0].Specs2[0].ProductId", product_id)
    add("Specification.Locations[0].Specs2[0].ProductId", product_id)
    add("Specification.Locations[0].Specs2[0].PreviousSelectedProductId", product_id)
    add("Specification.Locations[0].Specs2[0].PreviousSelectedUomId", uom_id)

    # Detect if product is "Other" - changes quantity to price (dollar) format
    is_other_product = product_matched.strip().lower() == "other"

    if is_other_product:
        # ProductDescription field only appears for "Other" products
        add("Specification.Locations[0].Specs2[0].ProductDescription", water_heater_number)
        # Quantity is the water heater price in dollar format (UoM = 368 = "Price")
        wh_price = float(water_heater_price) if water_heater_price else 0
        add("Specification.Locations[0].Specs2[0].Quantity", str(wh_price))
        add("Specification_Locations_0__Specs2_0__Quantitydollar", f"${wh_price:.2f}")
    else:
        # Standard product: quantity is a count
        add("Specification.Locations[0].Specs2[0].Quantity", str(quantity))
        add("Specification_Locations_0__Specs2_0__Quantityquantity", f"{float(quantity):.2f}")

    if install_method_id:
        add("Specification.Locations[0].Specs2[0].InstallMethodId", install_method_id)

    # --- DynamicAssortments ---

    # Group 0: Miscellaneous Materials (Sundry) - "Other" with materials price
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].GroupName", "Miscellaneous Materials")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].SelectedCategoryItem", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].MeasurementType", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].SelectedItem", materials_item_id)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].Description", "Materials")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].MeasurementType", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].MaterialType", materials_material_type)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].GroupKey", materials_group_key)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].IsSureTaxReceiver", "False")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].CloudProductId", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].CloudShapeId", "")
    add("UoM", "Price")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].Quantity", str(materials_price))
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[0].Quantitydollar", f"${float(materials_price):.2f}")
    add("GroupName", "Miscellaneous Materials")
    add("SelectedCategoryItem", "")
    add("MeasurementType", "")

    # Group 0 blank slot (index 4)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[4].SelectedItem", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[4].MeasurementType", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[4].MaterialType", materials_material_type)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[4].GroupKey", materials_group_key)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[4].IsSureTaxReceiver", "False")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[4].CloudProductId", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[4].CloudShapeId", "")
    add("UoM", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[4].Quantity", "0")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[4].Quantityquantity", "0.00")

    # Group 1: Water Heater Installation Labor (Accessory) - labor price
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].GroupName", "Water Heater Installation Labor")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].SelectedCategoryItem", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].MeasurementType", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].SelectedItem", labor_item_id)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].MeasurementType", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].MaterialType", labor_material_type)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].GroupKey", labor_group_key)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].IsSureTaxReceiver", "False")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].CloudProductId", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].CloudShapeId", "")
    add("UoM", "Price")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].Quantity", str(labor_price))
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[1].Quantitydollar", f"${float(labor_price):.2f}")
    add("GroupName", "Water Heater Installation Labor")
    add("SelectedCategoryItem", "")
    add("MeasurementType", "")

    # Group 1 blank slot (index 3)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[3].SelectedItem", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[3].MeasurementType", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[3].MaterialType", labor_material_type)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[3].GroupKey", labor_group_key)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[3].IsSureTaxReceiver", "False")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[3].CloudProductId", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[3].CloudShapeId", "")
    add("UoM", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[3].Quantity", "0")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[3].Quantityquantity", "0.00")

    # Group 2: Extended Protection Plan (Accessory) - default/none
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].GroupName", "Extended Protection Plan")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].SelectedCategoryItem", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].MeasurementType", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].SelectedItem", protection_item_id)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].MeasurementType", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].MaterialType", protection_material_type)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].GroupKey", protection_group_key)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].IsSureTaxReceiver", "False")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].CloudProductId", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].CloudShapeId", "")
    add("UoM", "Each")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].Quantity", "1")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[2].Quantityquantity", "1.00")
    add("GroupName", "Extended Protection Plan")
    add("SelectedCategoryItem", "")
    add("MeasurementType", "")

    # Group 2 blank slot (index 5)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[5].SelectedItem", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[5].MeasurementType", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[5].MaterialType", protection_material_type)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[5].GroupKey", protection_group_key)
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[5].IsSureTaxReceiver", "False")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[5].CloudProductId", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[5].CloudShapeId", "")
    add("UoM", "")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[5].Quantity", "0")
    add("Specification.Locations[0].Specs2[0].DynamicAssortments[5].Quantityquantity", "0.00")

    # Product selection forced + comments
    add("Specification.Locations[0].Specs2[0].ProductSelectionForced", "False")
    add("Specification.Locations[0].Comments", "")
    add("Project.IsAffiliatesOwnSpecSheet", "False")

    # Submit action
    add("saveAndViewEstimate", "saveAndViewEstimate")
    add("projectRoomId", "undefined")

    # --- Submit ---
    submit_url = f"{base}/GenericEnhancementProjectRoom/Create"
    referer = f"{base}/GenericEnhancementProjectRoom/Create?returnUrl={quote(return_url, safe='')}&workOrderId={internal_wo_id}"

    submit_headers = {
        **common_headers,
        "Origin": base,
        "Referer": referer,
    }

    resp = s.post(
        submit_url,
        data=form_pairs,
        headers=submit_headers,
        timeout=30,
    )

    if _is_login_page(resp):
        return {"status_code": 401, "body": {"error": "Session expired"}}

    # Parse response -- the Create POST returns JSON with newProjectId, returnUrl, errorMessage
    response_text = resp.text
    status = resp.status_code

    result = {
        "work_order_number": work_order_number,
        "internal_work_order_id": internal_wo_id,
        "project_name": project_name,
        "category": category_name,
        "location": location_name,
        "collection": collection_matched,
        "product": product_matched,
        "water_heater_number": water_heater_number,
        "water_heater_price": float(water_heater_price) if water_heater_price else None,
        "materials_price": materials_price,
        "labor_price": labor_price,
    }

    # Try JSON parse first (the JS CustomSubmitResponse expects JSON)
    project_id = None
    try:
        resp_json = json.loads(response_text)
        if resp_json.get("errorMessage"):
            return {
                "status_code": 400,
                "body": {"error": resp_json["errorMessage"], **result},
            }
        # newProjectId may be null; extract from returnUrl instead
        project_id = resp_json.get("newProjectId")
        if not project_id and resp_json.get("returnUrl"):
            pid_match = re.search(r'projectId=(\d+)', resp_json["returnUrl"])
            if pid_match:
                project_id = pid_match.group(1)
        if project_id:
            result["estimate_id"] = int(project_id)
    except (json.JSONDecodeError, ValueError):
        # Fallback: regex extraction from HTML/text
        proj_match = re.search(r'projectId=(\d+)', response_text)
        if proj_match:
            project_id = proj_match.group(1)
            result["estimate_id"] = int(project_id)
        else:
            proj_match = re.search(r'"newProjectId"\s*:\s*(\d+)', response_text)
            if proj_match:
                project_id = proj_match.group(1)
                result["estimate_id"] = int(project_id)

    if status not in (200, 302):
        return {
            "status_code": status,
            "body": {"error": f"Unexpected status {status}", "response_snippet": response_text[:500], **result},
        }

    result["status"] = "success"

    # -----------------------------------------------------------------------
    # Step 6: Fetch estimate view page for subtotal / tax / total
    # -----------------------------------------------------------------------
    view_html = None
    if project_id:
        try:
            view_url = f"{base}/ProjectEstimate/View?projectId={project_id}&returnUrl={quote(return_url, safe='')}"
            view_resp = s.get(view_url, timeout=30)
            view_html = view_resp.text

            # Parse Subtotal: <label ...>Subtotal</label> ... <span ...>$</span> 301.00
            subtotal_match = re.search(
                r'<label[^>]*>\s*Subtotal\s*</label>\s*<div[^>]*>\s*<span[^>]*>\$</span>\s*([\d,]+\.?\d*)',
                view_html,
            )
            if subtotal_match:
                result["subtotal"] = float(subtotal_match.group(1).replace(",", ""))

            # Parse Sales Tax: <label ...>Sales Tax</label> ... <span ...>$</span> 16.50
            tax_match = re.search(
                r'<label[^>]*>\s*Sales Tax\s*</label>\s*<div[^>]*>\s*<span[^>]*>\$</span>\s*([\d,]+\.?\d*)',
                view_html,
            )
            if tax_match:
                result["sales_tax"] = float(tax_match.group(1).replace(",", ""))

            # Parse Project Total: "Project Total $317.50" or similar
            total_match = re.search(
                r'Project\s+Total\s*\$\s*([\d,]+\.?\d*)',
                view_html,
            )
            if total_match:
                result["project_total"] = float(total_match.group(1).replace(",", ""))
        except Exception:
            # Non-fatal -- return success even if we can't fetch subtotals
            pass

    # -----------------------------------------------------------------------
    # Step 7: Send Estimate to customer
    # The "Send Estimate" button submits the ProjectEstimate form with submitAction=Apprun.
    # We parse the form fields from the view page HTML and POST them.
    # -----------------------------------------------------------------------
    if project_id and view_html:
        try:
            # Extract CSRF token from the view page form
            send_csrf = _extract_token(view_html)
            if send_csrf:
                # Parse the form fields from the view page
                view_parser = _parse_html(view_html)
                view_fields = _hidden_dict(view_parser)

                # Get RoomIds (may be multiple)
                room_ids = []
                for name, val in view_parser.inputs:
                    if name.startswith("RoomIds[") and val:
                        room_ids.append((name, val))

                # Get FinanceProgramId (selected option from dropdown)
                finance_program_id = ""
                fp_select = "ProjectEstimate.FinanceProgramId"
                fp_options = view_parser.selects.get(fp_select, [])
                for val, text, sel in fp_options:
                    if sel and val:
                        finance_program_id = val
                        break
                if not finance_program_id and fp_options:
                    for val, text, _ in fp_options:
                        if val:
                            finance_program_id = val
                            break

                # Build the Send Estimate form data
                send_pairs = []
                send_pairs.append(("__RequestVerificationToken", send_csrf))
                send_pairs.append(("ForceSendEstimate", view_fields.get("ForceSendEstimate", "False")))
                send_pairs.append(("ReturnUrl", view_fields.get("ReturnUrl", return_url)))
                send_pairs.append(("ProjectId", str(project_id)))
                send_pairs.append(("ProjectEstimate.WorkOrderId", view_fields.get("ProjectEstimate.WorkOrderId", work_order_number)))
                for room_name, room_val in room_ids:
                    send_pairs.append((room_name, room_val))
                send_pairs.append(("ProjectEstimate.EstimatePriceBreakdownType", view_fields.get("ProjectEstimate.EstimatePriceBreakdownType", "1")))
                send_pairs.append(("ProjectEstimate.DepositId", view_fields.get("ProjectEstimate.DepositId", "22")))
                send_pairs.append(("ProjectEstimate.IsFinancingProgramLinkedWithPromotion", view_fields.get("ProjectEstimate.IsFinancingProgramLinkedWithPromotion", "False")))
                send_pairs.append(("ProjectEstimate.FinanceProgramId", finance_program_id))
                send_pairs.append(("ProjectEstimate.PromoIdAdded", view_fields.get("ProjectEstimate.PromoIdAdded", "")))
                send_pairs.append(("ProjectEstimate.PromoIdRemoved", view_fields.get("ProjectEstimate.PromoIdRemoved", "")))
                send_pairs.append(("ProjectEstimate.ApplyBestValue", view_fields.get("ProjectEstimate.ApplyBestValue", "False")))
                send_pairs.append(("ProjectEstimate.PromotionCode", view_fields.get("ProjectEstimate.PromotionCode", "")))
                send_pairs.append(("IsAffiliatesOwnSpecSheet", view_fields.get("IsAffiliatesOwnSpecSheet", "False")))
                send_pairs.append(("submitAction", "Apprun"))

                # POST to /ProjectEstimate/View (no query string on POST)
                send_url = f"{base}/ProjectEstimate/View"
                send_headers = {
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "*/*",
                    "Origin": base,
                    "Referer": view_url,
                }
                send_resp = s.post(send_url, data=send_pairs, headers=send_headers, timeout=60)

                if _is_login_page(send_resp):
                    result["send_estimate"] = "failed"
                    result["send_error"] = "Session expired during send"
                elif send_resp.status_code == 200:
                    result["send_estimate"] = "sent"
                else:
                    result["send_estimate"] = "failed"
                    result["send_error"] = f"HTTP {send_resp.status_code}"
        except Exception as e:
            result["send_estimate"] = "failed"
            result["send_error"] = str(e)

    return {"status_code": 200, "body": result}

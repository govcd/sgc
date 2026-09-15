"""
Savar Government College (SGC) Multi-Batch Automation Scraper
Website: https://savargc.eshiksabd.com
Credentials: savarstudent / savarstudent
Supported Batches: HSC-19 to HSC-27 (2017-2018 to 2025-2026)
"""

import sys
import os
import re
import io
import time
import json
import argparse
import requests
import pdfplumber
import pandas as pd
from PIL import Image
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://savargc.eshiksabd.com"
REFERER  = f"{BASE_URL}/Result-Enquiry-Center"
COLLEGE_CODE = "sgc"
COLLEGE_NAME = "Savar Government College"
FIXED_PREFIX = ""
ROLL_DIGITS = 3
HAS_APPLICATION_FORM_DEFAULT = False
GROUPS = {
    "Science": {"group_code": "01", "letter": "s", "subfolder": "science"},
    "Humanities": {"group_code": "02", "letter": "h", "subfolder": "humanities"},
    "Business Studies": {"group_code": "03", "letter": "b", "subfolder": "bstudies"}
}

BATCH_CONFIGS = {
    "hsc27": {"session": "2526", "label": "2025-2026 (HSC-27)"},
    "hsc26": {"session": "2425", "label": "2024-2025 (HSC-26)"},
    "hsc25": {"session": "2324", "label": "2023-2024 (HSC-25)"},
    "hsc24": {"session": "2223", "label": "2022-2023 (HSC-24)"},
    "hsc23": {"session": "2122", "label": "2021-2022 (HSC-23)"},
    "hsc22": {"session": "2021", "label": "2020-2021 (HSC-22)"},
    "hsc21": {"session": "1920", "label": "2019-2020 (HSC-21)"},
    "hsc20": {"session": "1819", "label": "2018-2019 (HSC-20)"},
    "hsc19": {"session": "1718", "label": "2017-2018 (HSC-19)"},
}

COLUMN_ORDER = [
    "college_roll", "short_roll", "admission_roll", "student_name_en", "student_name_bn",
    "group_name", "batch", "session", "section", "practical_group", "student_phone",
    "father_name_en", "father_name_bn", "mother_name_en", "mother_name_bn",
    "ssc_roll", "ssc_reg", "ssc_gpa", "ssc_board", "ssc_year",
    "fourth_subject", "elective_subjects", "all_subjects",
    "blood_group", "gender", "date_of_birth", "religion", "quota",
    "student_email", "nid_birth_reg",
    "father_phone", "father_nid", "father_occupation", "father_annual_income",
    "mother_phone", "mother_nid",
    "present_address", "present_district", "permanent_address", "permanent_district",
    "local_guardian", "nationality", "admission_fee",
    "transaction_no", "bank_name", "payment_date", "payment_mode",
    "receipt_slip_url", "app_pdf_url", "pdf_filename", "photo_filename", "photo_web_url"
]

def calculate_section_info(college_code, group_name, roll_num):
    if college_code == "dc":
        if group_name == "Science":
            sec_idx = min(5, (roll_num - 1) // 150)
            sec_letter = chr(ord('A') + sec_idx)
            prac_sub = 1 if ((roll_num - 1) % 150) < 75 else 2
            return sec_letter, f"{sec_letter}{prac_sub}"
        elif group_name == "Business Studies":
            return "B. Studies", "BS"
        else:
            return "Humanities", "HUM"
    return group_name, f"{group_name[:3].upper()}-{roll_num}"

class PortalSession:
    def __init__(self):
        self.session = requests.Session()
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0"
        self.cookies = []
        self.authenticate()

    def authenticate(self):
        print(f"[*] Authenticating with {COLLEGE_NAME} portal ({BASE_URL})...")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(BASE_URL, timeout=45000)
            page.locator("input[name=username]").fill("savarstudent")
            page.locator("input[name=password]").fill("savarstudent")
            page.locator("input[type=submit]").click()
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(2)
            self.cookies = context.cookies()
            self.user_agent = page.evaluate("navigator.userAgent")
            browser.close()

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Referer": REFERER,
            "Origin": BASE_URL,
            "X-Requested-With": "XMLHttpRequest"
        })
        for c in self.cookies:
            self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        print("[+] Authentication successful.")

    def post(self, url, data=None, timeout=25, retries=2):
        for attempt in range(retries + 1):
            try:
                resp = self.session.post(url, data=data, timeout=timeout)
                if "login" in resp.url.lower() or resp.status_code in (401, 403):
                    print("[!] Session expired. Re-authenticating...")
                    self.authenticate()
                    resp = self.session.post(url, data=data, timeout=timeout)
                return resp
            except Exception as e:
                if attempt == retries:
                    raise e
                time.sleep(1.5)

    def get(self, url, timeout=25, retries=2):
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(url, timeout=timeout)
                if "login" in resp.url.lower() or resp.status_code in (401, 403):
                    print("[!] Session expired. Re-authenticating...")
                    self.authenticate()
                    resp = self.session.get(url, timeout=timeout)
                return resp
            except Exception as e:
                if attempt == retries:
                    raise e
                time.sleep(1.5)

def save_image_as_jpg(image_bytes, target_path):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(target_path, "JPEG", quality=95)
    except Exception:
        target_path.write_bytes(image_bytes)

def parse_slip_pdf(pdf_bytes):
    data = {
        "admission_roll": "",
        "father_name": "",
        "mother_name": "",
        "ssc_reg": "",
        "student_phone": "",
    }
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages:
                return data
            text = pdf.pages[0].extract_text() or ""
        m = re.search(r"Admission\s*Roll\s*:\s*(\d+)", text)
        if m: data["admission_roll"] = m.group(1).strip()
        m = re.search(r"Father'?s?\s*Name\s*:\s*([^:\n]+?)(?:Class\s*Roll|\n|$)", text)
        if m: data["father_name"] = m.group(1).strip()
        m = re.search(r"Mother'?s?\s*Name\s*:\s*([^:\n]+?)(?:SSC/HSC|\n|$)", text)
        if m: data["mother_name"] = m.group(1).strip()
        m = re.search(r"SSC/HSC\s*Reg\.?\s*No\.?\s*:\s*([0-9]+)?", text)
        if m and m.group(1): data["ssc_reg"] = m.group(1).strip()
        m = re.search(r"Student\s*Phone\s*:\s*([0-9]+)?", text)
        if m and m.group(1): data["student_phone"] = m.group(1).strip()
    except Exception as e:
        print(f"      [!] Error parsing slip PDF: {e}")
    return data

def parse_application_pdf(pdf_bytes):
    fields = {}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages:
                return fields
            text = pdf.pages[0].extract_text() or ""
        
        m = re.search(r"Admission Roll:\s*(\d+)", text)
        if m: fields["admission_roll"] = m.group(1).strip()

        m = re.search(r"01\.\s*Student Name \(English\)\s*:\s*([^(\n]+)(?:\(বাংলায়\)\s*:\s*([^\n]+))?", text)
        if m:
            fields["student_name_en"] = m.group(1).strip()
            fields["student_name_bn"] = (m.group(2) or "").strip()

        m = re.search(r"Student'?s? NID/Birth Reg\.\s*:\s*([0-9]+)?\s*Gender\s*:\s*([A-Za-z]+)?", text)
        if m:
            fields["nid_birth_reg"] = (m.group(1) or "").strip()
            fields["gender"] = (m.group(2) or "").strip()

        m = re.search(r"Student'?s? Phone\s*:\s*([0-9]+)", text)
        if m: fields["student_phone"] = m.group(1).strip()

        m = re.search(r"Student'?s? E-?mail\s*:\s*([^\s\n]+@[^\s\n]+)?", text)
        if m and m.group(1): fields["student_email"] = m.group(1).strip()

        m = re.search(r"02\.\s*Father'?s? Name\s*:\s*([^(\n]+)(?:\(বাংলায়\)\s*:\s*([^\n]+))?", text)
        if m:
            fields["father_name_en"] = m.group(1).strip()
            fields["father_name_bn"] = (m.group(2) or "").strip()

        m = re.search(r"Father'?s?/Guardian'?s? NID\s*:\s*([0-9]+)", text)
        if m: fields["father_nid"] = m.group(1).strip()

        m = re.search(r"Father'?s?/Guardian'?s? Phone\s*:\s*([0-9]+)", text)
        if m: fields["father_phone"] = m.group(1).strip()

        m = re.search(r"03\.\s*Mother'?s? Name\s*:\s*([^(\n]+)(?:\(বাংলায়\)\s*:\s*([^\n]+))?", text)
        if m:
            fields["mother_name_en"] = m.group(1).strip()
            fields["mother_name_bn"] = (m.group(2) or "").strip()

        m = re.search(r"Mother'?s? NID\s*:\s*([0-9]+)", text)
        if m: fields["mother_nid"] = m.group(1).strip()

        m = re.search(r"Mother'?s? Phone\s*:\s*([0-9]+)", text)
        if m: fields["mother_phone"] = m.group(1).strip()

        m = re.search(r"04\.\s*Permanent Address\s*:\s*(.*?)(?=District:)", text, re.DOTALL)
        m_pdist = re.search(r"04\..*?District:\s*([A-Za-z]+)", text)
        if m: fields["permanent_address"] = " ".join(m.group(1).split())
        if m_pdist: fields["permanent_district"] = m_pdist.group(1).strip()

        m = re.search(r"05\.\s*Present Address\s*:\s*(.*?)(?=District:)", text, re.DOTALL)
        m_pres_dist = re.search(r"05\..*?District:\s*([A-Za-z]+)", text)
        if m: fields["present_address"] = " ".join(m.group(1).split())
        if m_pres_dist: fields["present_district"] = m_pres_dist.group(1).strip()

        m = re.search(r"06\.\s*Local Guardian Name, Address & Phone\s*:\s*([^\n]+)", text)
        if m: fields["local_guardian"] = m.group(1).strip()

        m = re.search(r"07\.\s*Nationality\s*:\s*([A-Za-z]+)", text)
        if m: fields["nationality"] = m.group(1).strip()

        m = re.search(r"08\.\s*Father'?s? Occupation\s*:\s*(.*?)\s*11\.\s*Father'?s? Annual Income\s*:\s*([0-9]+)?", text)
        if m:
            fields["father_occupation"] = m.group(1).strip()
            fields["father_annual_income"] = (m.group(2) or "").strip()

        m_dob = re.search(r"09\.\s*Date of Birth\s*:\s*([0-9A-Za-z\-]+)", text)
        if m_dob: fields["date_of_birth"] = m_dob.group(1).strip()

        m_quota = re.search(r"12\.\s*Quota\s*:\s*([^\n\r]+)", text)
        if m_quota:
            q_val = m_quota.group(1).strip()
            if not any(k in q_val for k in ["Religion", "Blood", "10.", "13."]):
                fields["quota"] = q_val

        m_rel = re.search(r"10\.\s*Religion\s*:\s*([A-Za-z]+)", text)
        if m_rel: fields["religion"] = m_rel.group(1).strip()

        m_bld = re.search(r"13\.\s*Blood Group\s*:\s*([A-Za-z0-9+\-]+)", text)
        if m_bld: fields["blood_group"] = m_bld.group(1).strip()

        m = re.search(r"SSC\s+(\d+)\s+(\d+)\s+([0-9\-]+)\s+([A-Za-z\s\-]+?)\s+(\d{4})\s+([A-Za-z]+)\s+([0-9.]+)", text)
        if m:
            fields["ssc_roll"] = m.group(1)
            fields["ssc_reg"] = m.group(2)
            fields["ssc_year"] = m.group(5)
            fields["ssc_board"] = m.group(6)
            fields["ssc_gpa"] = m.group(7)

        sub_lines = []
        in_sub = False
        for line in text.split("\n"):
            if "15. Subject List" in line:
                in_sub = True
                continue
            if in_sub:
                if "16." in line or "Admission Fee" in line:
                    break
                if line.strip() and "1st Paper" not in line:
                    sub_lines.append(line.strip())
        if sub_lines:
            fields["all_subjects"] = "; ".join(sub_lines)

        m = re.search(r"17\.\s*Admission Fee:\s*([^\n]+)", text)
        if m: fields["admission_fee"] = m.group(1).strip()
    except Exception as e:
        print(f"      [!] Error parsing Application PDF: {e}")
    return fields

def save_database(records, excel_path, csv_path, json_path):
    if not records:
        return
    df = pd.DataFrame(records)
    for col in COLUMN_ORDER:
        if col not in df.columns:
            df[col] = ""
    df = df[COLUMN_ORDER]
    df.to_excel(excel_path, index=False)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

def scrape_single_batch(session, batch_key, resume=True):
    cfg_batch = BATCH_CONFIGS.get(batch_key)
    if not cfg_batch:
        print(f"[!] Unknown batch: {batch_key}")
        return 0

    session_code = cfg_batch["session"]
    batch_label = cfg_batch["label"]
    roll_prefix = f"{FIXED_PREFIX}{session_code}"

    root_dir = Path(__file__).resolve().parent
    batch_dir = root_dir / batch_key
    pdfs_dir = batch_dir / "pdfs"
    photos_dir = batch_dir / "photos"
    data_dir = batch_dir / "data"

    for grp in ["science", "bstudies", "humanities"]:
        (pdfs_dir / grp).mkdir(parents=True, exist_ok=True)
        (photos_dir / grp).mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    excel_path = data_dir / f"{batch_key}.xlsx"
    csv_path   = data_dir / f"{batch_key}.csv"
    json_path  = data_dir / f"{batch_key}.json"

    print("=" * 70)
    print(f"   SCRAPING: {COLLEGE_NAME.upper()} ({COLLEGE_CODE.upper()}) | {batch_label}")
    print("=" * 70)
    print(f"Output Directory: {batch_dir}")
    print(f"Roll Prefix: {roll_prefix}")
    print(f"Cutoff Rule: 20 consecutive empty rolls per group.")
    print("=" * 70)

    records = []
    processed_rolls = set()
    if resume and json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                records = json.load(f)
            processed_rolls = {r.get("college_roll") for r in records}
            print(f"[*] Resumed: {len(records)} students already in database for {batch_key}.")
        except Exception as e:
            print(f"[!] Could not load previous records: {e}")

    total_batch_students = len(records)
    batch_has_any_data = len(records) > 0

    for group_name, cfg in GROUPS.items():
        grp_code = cfg["group_code"]
        letter = cfg["letter"]
        subfolder = cfg["subfolder"]
        photo_sub_dir = photos_dir / subfolder
        pdf_sub_dir = pdfs_dir / subfolder

        print(f"\n>>> Batch: {batch_key.upper()} | Group: {group_name} (Code: {grp_code}) <<<")
        
        consecutive_misses = 0
        consecutive_tx_misses = 0
        consecutive_app_misses = 0
        receipts_available = True
        app_reprint_available = HAS_APPLICATION_FORM_DEFAULT
        roll_num = 1

        while True:
            short_roll = str(roll_num).zfill(ROLL_DIGITS)
            full_roll = f"{roll_prefix}{grp_code}{short_roll}"
            photo_fname = f"{short_roll}-{letter}-{COLLEGE_CODE}.jpg"
            pdf_fname   = f"{short_roll}-{letter}-{COLLEGE_CODE}.pdf"
            photo_path  = photo_sub_dir / photo_fname
            pdf_path    = pdf_sub_dir / pdf_fname

            if full_roll in processed_rolls:
                roll_num += 1
                consecutive_misses = 0
                continue

            # 1. Profile Check
            try:
                r_prof = session.post(f"{BASE_URL}/controller_student_module.php", data={
                    "rootData": full_roll,
                    "flagreq": "profileRollCheck"
                })
            except Exception as e:
                print(f"[!] Network error on {full_roll}: {e}")
                time.sleep(2)
                continue

            html_prof = r_prof.text
            if "studentClassRoll" not in html_prof or "No such class roll exists" in html_prof:
                consecutive_misses += 1
                if consecutive_misses % 5 == 0 or consecutive_misses >= 18:
                    print(f"  [-] Roll {full_roll} not found ({consecutive_misses}/20)")
                if consecutive_misses >= 20:
                    found_in_group = roll_num - 20
                    print(f"[✓] Reached 20 consecutive empty rolls. Finished {group_name}. Valid seats: {found_in_group}")
                    break
                roll_num += 1
                continue

            consecutive_misses = 0
            batch_has_any_data = True
            
            m_name = re.search(r"Name\s*:</label></div><div class=\"col-sm-9\"><label>([^<]+)</label>", html_prof)
            student_name_en = m_name.group(1).strip() if m_name else ""
            
            section, prac_grp = calculate_section_info(COLLEGE_CODE, group_name, roll_num)

            fourth_subject = ""
            elective_subjects = []
            all_subjects_list = []
            sub_block = re.search(r"<tbody id=\"studentSubjectBody\">(.*?)</tbody>", html_prof, re.DOTALL)
            if sub_block:
                for p1, p2, stype in re.findall(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", sub_block.group(1), re.DOTALL):
                    p1 = p1.strip()
                    p2 = p2.strip()
                    stype = stype.strip()
                    sub_str = f"{p1}" + (f" / {p2}" if p2 else "")
                    all_subjects_list.append(f"{sub_str} ({stype})")
                    if "4th" in stype.lower() or "fourth" in stype.lower():
                        fourth_subject = sub_str
                    elif "elective" in stype.lower() or "compulsory" in stype.lower():
                        elective_subjects.append(sub_str)

            photo_web_url = ""
            m_img = re.search(r"src=[\"\'](image/student/[^\"]+)[\"\']", html_prof)
            if m_img:
                photo_web_url = f"{BASE_URL}/{m_img.group(1).lstrip('/')}"
                if not (photo_path.exists() and photo_path.stat().st_size > 500):
                    try:
                        r_img = session.get(photo_web_url, timeout=15)
                        if r_img.status_code == 200 and len(r_img.content) > 300:
                            save_image_as_jpg(r_img.content, photo_path)
                    except Exception as e:
                        pass

            student_rec = {
                "college_roll": full_roll,
                "short_roll": short_roll,
                "admission_roll": "",
                "student_name_en": student_name_en,
                "student_name_bn": "",
                "group_name": group_name,
                "batch": batch_key.upper(),
                "session": batch_label,
                "section": section,
                "practical_group": prac_grp,
                "student_phone": "",
                "father_name_en": "",
                "father_name_bn": "",
                "mother_name_en": "",
                "mother_name_bn": "",
                "ssc_roll": "",
                "ssc_reg": "",
                "ssc_gpa": "",
                "ssc_board": "",
                "ssc_year": "",
                "fourth_subject": fourth_subject,
                "elective_subjects": "; ".join(elective_subjects),
                "all_subjects": "; ".join(all_subjects_list),
                "blood_group": "",
                "gender": "",
                "date_of_birth": "",
                "religion": "",
                "quota": "",
                "student_email": "",
                "nid_birth_reg": "",
                "father_phone": "",
                "father_nid": "",
                "father_occupation": "",
                "father_annual_income": "",
                "mother_phone": "",
                "mother_nid": "",
                "present_address": "",
                "present_district": "",
                "permanent_address": "",
                "permanent_district": "",
                "local_guardian": "",
                "nationality": "",
                "admission_fee": "",
                "transaction_no": "",
                "bank_name": "",
                "payment_date": "",
                "payment_mode": "",
                "receipt_slip_url": "",
                "app_pdf_url": "",
                "pdf_filename": "",
                "photo_filename": photo_fname if photo_path.exists() else "",
                "photo_web_url": photo_web_url
            }

            # 2. Receipts Print Check (transactionCheck)
            if receipts_available:
                try:
                    r_tx = session.post(f"{BASE_URL}/controller_student_module.php", data={
                        "rootData": full_roll,
                        "flagreq": "transactionCheck"
                    })
                    html_tx = r_tx.text
                    receipt_matches = re.findall(r"printReceipts\((\d+)\)", html_tx)
                    if receipt_matches:
                        consecutive_tx_misses = 0
                        target_id = receipt_matches[-1]
                        for row in html_tx.split("</tr>"):
                            if "Admission" in row:
                                m = re.search(r"printReceipts\((\d+)\)", row)
                                if m:
                                    target_id = m.group(1)
                                    break
                        
                        m_row = re.search(r"<tr>\s*<td>[^<]*</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>", html_tx)
                        if m_row:
                            student_rec["bank_name"] = m_row.group(1).strip()
                            student_rec["transaction_no"] = m_row.group(2).strip()
                            student_rec["payment_date"] = m_row.group(3).strip()
                            student_rec["payment_mode"] = m_row.group(4).strip()
                            student_rec["admission_fee"] = m_row.group(5).strip()

                        r_enc = session.post(f"{BASE_URL}/controller_student_module.php", data={
                            "rootData": target_id,
                            "flagreq": "ajaxEncryption"
                        })
                        xx_code = r_enc.text.strip()
                        if xx_code and "Error" not in xx_code:
                            slip_url = f"{BASE_URL}/std_coll_slip.php?xxCode={xx_code}"
                            student_rec["receipt_slip_url"] = slip_url
                            r_slip = session.get(slip_url)
                            pdf_idx = r_slip.content.find(b"%PDF")
                            if pdf_idx != -1:
                                slip_data = parse_slip_pdf(r_slip.content[pdf_idx:])
                                if slip_data.get("admission_roll"):
                                    student_rec["admission_roll"] = slip_data["admission_roll"]
                                if slip_data.get("student_phone"):
                                    student_rec["student_phone"] = slip_data["student_phone"]
                                if slip_data.get("father_name"):
                                    student_rec["father_name_en"] = slip_data["father_name"]
                                if slip_data.get("mother_name"):
                                    student_rec["mother_name_en"] = slip_data["mother_name"]
                                if slip_data.get("ssc_reg"):
                                    student_rec["ssc_reg"] = slip_data["ssc_reg"]
                    else:
                        consecutive_tx_misses += 1
                        if consecutive_tx_misses >= 15:
                            print(f"      [*] Note: Receipts Print not found for 15 consecutive students in {batch_key} ({group_name}). Skipping Receipts check.")
                            receipts_available = False
                except Exception as e:
                    pass

            # 3. Application Form Reprint Check
            adm_roll = student_rec.get("admission_roll")
            if adm_roll and app_reprint_available:
                try:
                    r_app = session.post(f"{BASE_URL}/controller_student_module.php", data={
                        "rootData": adm_roll,
                        "sessionID": "21",
                        "flagreq": "checkTransaction"
                    })
                    app_code = r_app.text.strip()
                    if app_code and "Error" not in app_code:
                        consecutive_app_misses = 0
                        app_pdf_url = f"{BASE_URL}/Student-Admission?xxCode={app_code}"
                        student_rec["app_pdf_url"] = app_pdf_url
                        r_app_pdf = session.get(app_pdf_url, timeout=30)
                        pdf_start = r_app_pdf.content.find(b"%PDF")
                        if pdf_start != -1:
                            pdf_bytes = r_app_pdf.content[pdf_start:]
                            pdf_path.write_bytes(pdf_bytes)
                            student_rec["pdf_filename"] = pdf_fname
                            app_fields = parse_application_pdf(pdf_bytes)
                            for k, v in app_fields.items():
                                if v:
                                    student_rec[k] = v
                    else:
                        consecutive_app_misses += 1
                        if consecutive_app_misses >= 15:
                            print(f"      [*] Note: Application Form Reprint not available for {batch_key} ({group_name}). Skipping Application Form check.")
                            app_reprint_available = False
                except Exception as e:
                    pass

            records.append(student_rec)
            processed_rolls.add(full_roll)
            total_batch_students += 1

            status_msg = f"[{short_roll}] {student_rec['student_name_en'][:22]:<22} | Adm: {student_rec['admission_roll'] or 'N/A'} | Phone: {student_rec['student_phone'] or 'N/A'}"
            if student_rec.get("pdf_filename"):
                status_msg += " | PDF [✓]"
            if photo_path.exists():
                status_msg += " | Photo [✓]"
            print(f"  [+] {status_msg}")

            if len(records) % 10 == 0:
                save_database(records, excel_path, csv_path, json_path)

            roll_num += 1

        save_database(records, excel_path, csv_path, json_path)

    save_database(records, excel_path, csv_path, json_path)
    print(f"\n[✓] {batch_label} Completed! Total records: {len(records)}")
    return len(records)

def main():
    parser = argparse.ArgumentParser(description=f"{COLLEGE_NAME} Multi-Batch Student Scraper")
    parser.add_argument("--batch", type=str, default=None, help="Batch to scrape: hsc27, hsc26, ... or 'all'")
    args = parser.parse_args()

    selected_batch = args.batch

    if not selected_batch:
        print("=" * 65)
        print(f"   {COLLEGE_NAME.upper()} ({COLLEGE_CODE.upper()}) MULTI-BATCH STUDENT DATABASE SCRAPER")
        print("=" * 65)
        keys = list(BATCH_CONFIGS.keys())
        for idx, k in enumerate(keys, 1):
            print(f"  [{idx}] {BATCH_CONFIGS[k]['label']}")
        print("  [A] ALL BATCHES (HSC-27 down to HSC-19 sequentially)")
        print("=" * 65)
        choice = input("Select batch to run (1-9 or A, default 1): ").strip().upper()
        if choice == "A":
            selected_batch = "all"
        elif choice.isdigit() and 1 <= int(choice) <= len(keys):
            selected_batch = keys[int(choice) - 1]
        else:
            selected_batch = "hsc27"

    session = PortalSession()

    if selected_batch.lower() == "all":
        print("[*] Running ALL batches from HSC-27 to HSC-19...")
        for b_key in BATCH_CONFIGS.keys():
            scrape_single_batch(session, b_key)
    else:
        batches_to_run = [b.strip().lower() for b in selected_batch.split(",") if b.strip()]
        for b_key in batches_to_run:
            scrape_single_batch(session, b_key)

    print("\n" + "=" * 70)
    print(f"[✓] ALL REQUESTED SCRAPING OPERATIONS FINISHED FOR {COLLEGE_NAME.upper()}!")
    print("=" * 70)

if __name__ == "__main__":
    main()

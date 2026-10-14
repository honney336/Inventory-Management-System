"""Drive the UI in a headless browser and report what the user actually sees.

    python scripts/ui_check.py http://127.0.0.1:5011 --out /tmp/shots
"""

import argparse
import os
import sys
import time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


ADMIN_PASSWORD = "Str0ngPassw0rd"


def build_driver(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--lang=en-GB")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)
    return driver


def shot(driver, out_dir, name):
    path = os.path.join(out_dir, f"{name}.png")
    driver.save_screenshot(path)
    return path


def flash_report(driver):
    """Describe the flash area the way a person would see it."""
    result = {"container": False, "items": [], "geometry": None, "issue": None}
    boxes = driver.find_elements(By.CSS_SELECTOR, ".alert")
    flashes = driver.find_elements(By.CSS_SELECTOR, ".flashes")
    result["container"] = bool(flashes)

    for box in boxes:
        text = box.text.strip()
        cls = box.get_attribute("class") or ""
        severity = cls.split("alert--")[-1].split()[0] if "alert--" in cls else "?"
        visible = box.is_displayed()
        rect = box.rect
        result["items"].append({
            "severity": severity,
            "text": text,
            "visible": visible,
            "box": {"x": rect["x"], "y": rect["y"], "w": rect["width"], "h": rect["height"]},
        })

    if boxes:
        rect = boxes[0].rect
        result["geometry"] = {"x": rect["x"], "y": rect["y"], "w": rect["width"]}

    main = driver.find_element(By.CSS_SELECTOR, "main.main")
    centred = "main--centered" in (main.get_attribute("class") or "")

    if result["items"] and flashes:
        flash_box = boxes[0].rect
        card = driver.find_elements(By.CSS_SELECTOR, ".auth-card")
        card_box = card[0].rect if card else None

        if centred and card_box and abs(flash_box["width"] - card_box["width"]) > 4:
            result["issue"] = (
                f"toast is {flash_box['width']:.0f}px wide, card is "
                f"{card_box['width']:.0f}px wide"
            )
        if centred and card_box and flash_box["y"] + flash_box["height"] > card_box["y"] - 4:
            result["issue"] = "flash stretches the full width of a centred page"
        if card_box and flash_box["y"] + flash_box["height"] > card_box["y"] + 4:
            result["issue"] = (
                "flash overlaps the card instead of sitting above it "
                f"(flash bottom {flash_box['y'] + flash_box['height']}, "
                f"card top {card_box['y']})"
            )
        if card_box and flash_box["x"] < card_box["x"] - 8:
            result["issue"] = (
                f"flash starts {card_box['x'] - flash_box['x']}px left of the card"
            )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("--out", default="/tmp/ui-shots")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--admin-password", default="Str0ngPassw0rd")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    base = args.base.rstrip("/")
    global ADMIN_PASSWORD
    ADMIN_PASSWORD = args.admin_password
    driver = build_driver(headless=not args.headed)
    wait = WebDriverWait(driver, 15)
    problems = []

    def token():
        return driver.find_element(By.NAME, "csrf_token").get_attribute("value")

    try:
        # 1. login page with a wrong password
        driver.get(f"{base}/auth/login")
        shot(driver, args.out, "01-login-clean")
        wait.until(EC.presence_of_element_located((By.NAME, "identity"))).send_keys("admin")
        driver.find_element(By.NAME, "password").send_keys("definitely-wrong")
        driver.execute_script("document.querySelector('[name=csrf_token]').value=arguments[0]", token())
        driver.find_element(By.CSS_SELECTOR, "form button[type=submit]").click()
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".alert")))
        time.sleep(0.4)
        shot(driver, args.out, "02-login-error-toast")
        report = flash_report(driver)
        print("login error toast:", report)
        if not report["items"]:
            problems.append("no toast rendered on a failed sign-in")
        if report["issue"]:
            problems.append(f"login toast layout: {report['issue']}")

        # 2. successful sign-in toast on the dashboard
        identity = driver.find_element(By.NAME, "identity")
        identity.clear()
        identity.send_keys("admin")
        driver.find_element(By.NAME, "password").send_keys(ADMIN_PASSWORD)
        driver.execute_script("document.querySelector('[name=csrf_token]').value=arguments[0]", token())
        driver.find_element(By.CSS_SELECTOR, "form button[type=submit]").click()
        try:
            wait.until(lambda drv: "login" not in drv.current_url)
        except TimeoutException:
            texts = [a.text.strip() for a in driver.find_elements(By.CSS_SELECTOR, ".alert")]
            if any("Too many failed sign-ins" in item for item in texts):
                raise SystemExit(
                    "the account is locked out after repeated failures. Wait for "
                    "LOGIN_LOCKOUT_MINUTES, or clear the login_attempts table:\n"
                    "  docker compose exec app flask --app app shell"
                )
            raise
        time.sleep(0.5)
        shot(driver, args.out, "03-dashboard-welcome-toast")
        report = flash_report(driver)
        print("welcome toast:", report)
        if not report["items"]:
            problems.append("no welcome toast after signing in")

        # 3. barcode label page
        driver.get(f"{base}/products/1/barcode")
        time.sleep(2.0)
        shot(driver, args.out, "04-barcode-label")
        canvas = driver.find_elements(By.CSS_SELECTOR, "#barcode")
        painted = False
        if canvas:
            painted = driver.execute_script(
                "const c=document.querySelector('#barcode');"
                "if(!c) return false;"
                "const ctx=c.getContext('2d');"
                "const d=ctx.getImageData(0,0,c.width,c.height).data;"
                "for(let i=0;i<d.length;i+=4){if(d[i+3]>0 && (d[i]<200)) return true;} return false;"
            )
        print("barcode canvas present:", bool(canvas), "painted:", painted)
        if not canvas or not painted:
            problems.append("barcode canvas is empty in the browser")

        # 4. scan desk
        driver.get(f"{base}/scan")
        time.sleep(1.5)
        shot(driver, args.out, "05-scan-desk")
        driver.find_element(By.ID, "codeInput").send_keys("CLN-100")
        driver.execute_script(
            "document.querySelector('#manualForm').dispatchEvent("
            "new Event('submit',{cancelable:true}))"
        )
        time.sleep(1.5)
        shot(driver, args.out, "06-scan-result")
        result_text = driver.find_element(By.ID, "result").text.strip()
        print("scan lookup result:", result_text[:80].replace("\n", " | "))
        if "CLN-100" not in result_text:
            problems.append("barcode/SKU lookup returned nothing in the browser")

        # 5. a form validation toast deeper in the app
        driver.get(f"{base}/products/new")
        time.sleep(0.5)
        driver.find_element(By.ID, "f-sku").send_keys("X")
        driver.find_element(By.ID, "f-name").send_keys("Browser check")
        driver.find_element(By.ID, "f-cost").send_keys("abc")
        price = driver.find_element(By.ID, "f-price")
        price.clear()
        price.send_keys("5")
        driver.find_element(By.ID, "f-reorder_point").send_keys("3")
        driver.find_element(By.ID, "f-reorder_quantity").send_keys("5")
        driver.execute_script("document.querySelectorAll('.form-actions button[type=submit]')[0].click()")
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".alert")))
        shot(driver, args.out, "07-product-validation-toast")
        report = flash_report(driver)
        print("product form toast:", report)
        if not report["items"]:
            problems.append("no toast after a rejected product form")

    finally:
        driver.quit()

    print()
    if problems:
        print("PROBLEMS FOUND:")
        for item in problems:
            print("  -", item)
    else:
        print("no layout problems detected")
    print("screenshots in", args.out)
    return len(problems)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
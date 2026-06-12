#!/usr/bin/env python3
"""Static file server + Rupayex payment API proxy."""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = int(os.environ.get("PORT", "8765"))


def load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_dotenv()

RUPAYEX_TOKEN = os.environ.get("RUPAYEX_TOKEN", "")
RUPAYEX_INSTANCE_ID = os.environ.get("RUPAYEX_INSTANCE_ID", "")
RUPAYEX_BASE = "https://rupayex.net/api"
SECUREPAY_BASE = "https://securepayy.store"


def parse_payment_page(payment_url):
    req = urllib.request.Request(payment_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    match = re.search(r"const SERVER = (\{.*?\});", html, re.DOTALL)
    if not match:
        return None
    return json.loads(match.group(1))


def payment_data_for_client(server_obj, payment_url=None):
    if not server_obj:
        return None
    return {
        "amount": server_obj.get("amount"),
        "order_id": server_obj.get("order_id"),
        "qr_code_url": server_obj.get("qrCodeUrl"),
        "upi_link": server_obj.get("upi_link"),
        "upi_id": server_obj.get("upi_id"),
        "paytm_intent": server_obj.get("paytmintent"),
        "phonepe_intent": server_obj.get("phonepeintent"),
        "payment_token": server_obj.get("token"),
        "redirect_url": server_obj.get("redirect_url"),
        "csrf": server_obj.get("csrf"),
        "payment_url": payment_url,
    }


def payment_base_from_url(payment_url):
    parsed = urllib.parse.urlparse(payment_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def post_securepay_form(base, path, form_dict, extra_headers=None, referer=None):
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)
    data = urllib.parse.urlencode(form_dict).encode("utf-8")
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP Error {e.code}: {err[:200] or e.reason}") from e


def proxy_rupayex(method, path, body=None):
    url = f"{RUPAYEX_BASE}{path}"
    headers = {
        "X-Api-Token": RUPAYEX_TOKEN,
        "X-Instance-Id": RUPAYEX_INSTANCE_ID,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = body.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body_text = resp.read().decode("utf-8")
        if not body_text.strip():
            return resp.status, json.dumps(
                {"status": False, "message": "Empty response from payment gateway"}
            )
        return resp.status, body_text


class Handler(SimpleHTTPRequestHandler):
    def send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def request_path(self):
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def read_json_payload(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        ct = self.headers.get("Content-Type", "")
        if "application/json" not in ct:
            raise ValueError("Content-Type must be application/json")
        return json.loads(raw or "{}")

    def handle_create_order(self):
        try:
            payload = self.read_json_payload()
        except (json.JSONDecodeError, ValueError) as e:
            self.send_json(400, {"status": False, "message": str(e)})
            return

        amount = payload.get("amount")
        order_id = payload.get("order_id")
        redirect_url = payload.get("redirect_url")
        if not amount or not order_id or not redirect_url:
            self.send_json(400, {
                "status": False,
                "message": "Missing required fields: amount, order_id, redirect_url",
            })
            return

        form = urllib.parse.urlencode({
            "user_token": RUPAYEX_TOKEN,
            "amount": amount,
            "order_id": order_id,
            "redirect_url": redirect_url,
            "customer_mobile": payload.get("customer_mobile", ""),
            "remark1": payload.get("remark1", ""),
        })

        try:
            status, body = proxy_rupayex("POST", "/create-order", form)
            try:
                data = json.loads(body) if body.strip() else {"status": False, "message": "Empty response"}
            except json.JSONDecodeError:
                data = {"status": False, "message": body}
            if data.get("status") and data.get("payment_url"):
                try:
                    server_obj = parse_payment_page(data["payment_url"])
                    payment_data = payment_data_for_client(server_obj, data["payment_url"])
                    if payment_data:
                        payment_data["payment_base"] = payment_base_from_url(data["payment_url"])
                        data["payment_data"] = payment_data
                except Exception as exc:
                    data["payment_parse_warning"] = str(exc)
                body = json.dumps(data)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            if err.strip():
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(err.encode("utf-8"))
            else:
                self.send_json(e.code, {"status": False, "message": f"Payment gateway error ({e.code})"})
        except Exception as e:
            self.send_json(500, {"status": False, "message": str(e)})

    def handle_submit_utr(self):
        try:
            payload = self.read_json_payload()
        except (json.JSONDecodeError, ValueError) as e:
            self.send_json(400, {"status": False, "message": str(e)})
            return
        for key in ("csrf", "order_id", "payment_token", "utr"):
            if not payload.get(key):
                self.send_json(400, {"status": False, "message": "Missing UTR fields"})
                return
        base = payload.get("payment_base") or SECUREPAY_BASE
        referer = payload.get("payment_url") or base
        try:
            status, body = post_securepay_form(base, "/pay/submit-utr", {
                "_token": payload["csrf"],
                "order_id": payload["order_id"],
                "payment_token": payload["payment_token"],
                "utr": payload["utr"],
            }, referer=referer)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except Exception as e:
            self.send_json(500, {"status": False, "message": str(e)})

    def handle_check_status(self):
        try:
            payload = self.read_json_payload()
        except (json.JSONDecodeError, ValueError) as e:
            self.send_json(400, {"status": False, "message": str(e)})
            return
        for key in ("csrf", "order_id", "payment_token"):
            if not payload.get(key):
                self.send_json(400, {"status": False, "message": "Missing status fields"})
                return
        base = payload.get("payment_base") or SECUREPAY_BASE
        referer = payload.get("payment_url") or base
        try:
            status, body = post_securepay_form(base, "/payment/check-status", {
                "_token": payload["csrf"],
                "order_id": payload["order_id"],
                "payment_token": payload["payment_token"],
            }, referer=referer)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except Exception as e:
            self.send_json(500, {"status": False, "message": str(e)})

    def handle_snapshot(self):
        try:
            payload = self.read_json_payload()
        except (json.JSONDecodeError, ValueError) as e:
            self.send_json(400, {"status": False, "message": str(e)})
            return
        if not payload.get("csrf") or not payload.get("order_id"):
            self.send_json(400, {"status": False, "message": "Missing snapshot fields"})
            return
        base = payload.get("payment_base") or SECUREPAY_BASE
        try:
            status, body = post_securepay_form(base, "/payment/snapshot", {
                "_token": payload["csrf"],
                "order_id": payload["order_id"],
            }, extra_headers={"X-CSRF-TOKEN": payload["csrf"]})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except Exception as e:
            self.send_json(500, {"status": False, "message": str(e)})

    def handle_refresh_payment(self):
        try:
            payload = self.read_json_payload()
        except (json.JSONDecodeError, ValueError) as e:
            self.send_json(400, {"status": False, "message": str(e)})
            return
        payment_url = payload.get("payment_url")
        if not payment_url:
            self.send_json(400, {"status": False, "message": "payment_url required"})
            return
        try:
            server_obj = parse_payment_page(payment_url)
            payment_data = payment_data_for_client(server_obj, payment_url)
            if not payment_data:
                self.send_json(500, {"status": False, "message": "Could not parse payment page"})
                return
            payment_data["payment_base"] = payment_base_from_url(payment_url)
            self.send_json(200, {"status": True, "payment_data": payment_data})
        except Exception as e:
            self.send_json(500, {"status": False, "message": str(e)})

    def do_POST(self):
        routes = {
            "/api/create-order": self.handle_create_order,
            "/api/submit-utr": self.handle_submit_utr,
            "/api/payment-check-status": self.handle_check_status,
            "/api/payment-snapshot": self.handle_snapshot,
            "/api/refresh-payment": self.handle_refresh_payment,
        }
        handler = routes.get(self.request_path())
        if handler:
            handler()
            return
        self.send_json(404, {"status": False, "message": "Not found"})

    def do_GET(self):
        if not self.path.startswith("/api/order-status"):
            return super().do_GET()

        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        order_id = params.get("order_id", [""])[0]
        if not order_id:
            self.send_json(400, {"status": False, "message": "order_id is required"})
            return

        qs = urllib.parse.urlencode({"user_token": RUPAYEX_TOKEN, "order_id": order_id})
        try:
            status, body = proxy_rupayex("GET", f"/order-status?{qs}")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            if err.strip():
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(err.encode("utf-8"))
            else:
                self.send_json(e.code, {"status": False, "message": f"Payment gateway error ({e.code})"})
        except Exception as e:
            self.send_json(500, {"status": False, "message": str(e)})


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving on http://localhost:{PORT}")
    print(f"Open: http://localhost:{PORT}/index.html")
    print(f"Test:  http://localhost:{PORT}/test-pay.html")
    server.serve_forever()

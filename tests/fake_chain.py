"""A local JSON-RPC chain for tests. It decodes real signed transactions and keeps balances, so kit and market code
runs unchanged. Set MAROO_RPC_URL to its URL. Anyone can be funded with fund(); status_of forces a failed receipt."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rlp
from eth_account import Account
from eth_utils import keccak, to_checksum_address


class FakeChain:
    def __init__(self, port):
        self.balances, self.nonces, self.txs, self.fail_to = {}, {}, {}, set()
        self.receipt_errors = 0                           # answer this many receipt reads with an error, as a lagging node does
        chain = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["content-length"])))
                try:
                    body = {"jsonrpc": "2.0", "id": req["id"], "result": chain.call(req["method"], req["params"])}
                except Exception as e:
                    body = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32000, "message": str(e)}}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()

    def fund(self, address, wei):
        self.balances[address] = self.balances.get(address, 0) + wei

    def call(self, method, params):
        if method == "eth_getBalance":
            return hex(self.balances.get(to_checksum_address(params[0]), 0))
        if method == "eth_getTransactionCount":
            return hex(self.nonces.get(to_checksum_address(params[0]), 0))
        if method == "eth_gasPrice":
            return hex(10 ** 13)
        if method == "eth_estimateGas":
            return hex(30_000)
        if method == "eth_sendRawTransaction":
            return self.send(params[0])
        if method == "eth_getTransactionByHash":
            return self.txs.get(params[0], {}).get("tx")
        if method == "eth_getTransactionReceipt":
            if self.receipt_errors:
                self.receipt_errors -= 1
                raise ValueError("tx not found")
            t = self.txs.get(params[0])
            return {"status": t["status"], "transactionHash": params[0]} if t else None
        raise ValueError(f"unexpected method {method}")

    def send(self, raw):
        data = bytes.fromhex(raw[2:])
        nonce, gas_price, gas, to, value, payload, *_ = rlp.decode(data)
        sender = Account.recover_transaction(raw)
        nonce = int.from_bytes(nonce, "big")
        if nonce != self.nonces.get(sender, 0):
            raise ValueError("nonce mismatch")
        value = int.from_bytes(value, "big")
        fee = int.from_bytes(gas, "big") * int.from_bytes(gas_price, "big")
        if self.balances.get(sender, 0) < value + fee:
            raise ValueError("insufficient funds")
        to = to_checksum_address(to)
        failed = to in self.fail_to
        self.balances[sender] -= fee + (0 if failed else value)
        if not failed:
            self.fund(to, value)
        self.nonces[sender] = nonce + 1
        tx_hash = "0x" + keccak(data).hex()
        self.txs[tx_hash] = {"status": "0x0" if failed else "0x1",
                             "tx": {"hash": tx_hash, "from": sender.lower(), "to": to.lower(), "value": hex(value),
                                    "input": "0x" + payload.hex()}}
        return tx_hash

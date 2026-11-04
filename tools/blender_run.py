import socket, json, sys

def send(cmd, timeout=600):
    s = socket.create_connection(('127.0.0.1', 9876), timeout=timeout)
    s.sendall(json.dumps(cmd).encode('utf-8'))
    buf = b''
    s.settimeout(timeout)
    while True:
        try:
            d = s.recv(65536)
        except socket.timeout:
            break
        if not d:
            break
        buf += d
        try:
            r = json.loads(buf.decode('utf-8', 'replace'))
            s.close()
            return r
        except Exception:
            continue
    s.close()
    return {"status": "error", "result": {"result": "no complete json: " + buf[:400].decode('utf-8', 'replace')}}

if __name__ == "__main__":
    code = open(sys.argv[1], encoding='utf-8').read()
    r = send({"type": "execute_code", "params": {"code": code}})
    print("STATUS:", r.get("status"))
    res = r.get("result", {})
    out = res.get("result") if isinstance(res, dict) else str(res)
    print(out if out else json.dumps(res, ensure_ascii=False)[:2000])

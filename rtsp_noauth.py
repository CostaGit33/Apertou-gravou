import socket

paths = ["/", "/live/ch00_0", "/stream0", "/cam/realmonitor?channel=1&subtype=0"]

for path in paths:
    try:
        url = f"rtsp://192.168.100.52:554{path}"
        req = (
            f"DESCRIBE {url} RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"User-Agent: ProbeClient\r\n\r\n"
        ).encode()

        s = socket.socket()
        s.settimeout(3)
        s.connect(("192.168.100.52", 554))
        s.sendall(req)
        resp = s.recv(2048).decode(errors="replace")
        s.close()
        print(f"--- {path} ---")
        print(resp[:300])
    except Exception as e:
        print(f"ERR: {e}")

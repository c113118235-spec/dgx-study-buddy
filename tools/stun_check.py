"""檢查 STUN 伺服器連不連得到、DGX 的對外位址是什麼（家裡或校外看不到影像時先跑這個）。

  docker exec xiaoqing python /app/tools/stun_check.py
STUN 是 WebRTC 用來找出「自己在網路外面看起來的位址」；伺服器端的 STUN 連不到，
校外的裝置就很難直接連上 DGX（只剩 TURN 中繼這條路）。
"""
import os
import socket
import struct

SERVERS = [("stun.freeswitch.org", 3478), ("stun.cloudflare.com", 3478), ("stun.l.google.com", 19302)]


def query(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    tid = os.urandom(12)
    s.sendto(struct.pack("!HHI", 1, 0, 0x2112A442) + tid, (host, port))
    data, _ = s.recvfrom(2048)
    i = 20
    while i + 4 <= len(data):                      # 找 XOR-MAPPED-ADDRESS
        t, ln = struct.unpack("!HH", data[i:i + 4])
        v = data[i + 4:i + 4 + ln]
        if t == 0x0020 and len(v) >= 8:
            p = struct.unpack("!H", v[2:4])[0] ^ 0x2112
            ip = ".".join(str(b ^ m) for b, m in zip(v[4:8], b"\x21\x12\xa4\x42"))
            return f"{ip}:{p}"
        i += 4 + ln + (-ln % 4)
    return "有回應"


for h, p in SERVERS:
    try:
        print(f"{h:22s} 可以，DGX 對外位址 {query(h, p)}")
    except Exception as e:
        print(f"{h:22s} 連不到（{e.__class__.__name__}）")

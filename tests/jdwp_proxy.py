"""JDWP logging proxy: debugger -> this proxy -> real JVM.

Logs every command packet (cmdset/cmd + data hex) in both directions.
Usage: echo dump | python3 jdwp_proxy.py <listen_port> <target_host> <target_port>
"""

import socket
import struct
import sys
import threading


def pipe(src, dst, direction, log):
    while True:
        hdr = b""
        try:
            while len(hdr) < 11:
                chunk = src.recv(11 - len(hdr))
                if not chunk:
                    return
                hdr += chunk
        except OSError:
            return
        length, pid = struct.unpack(">II", hdr[:8])
        flags = hdr[8]
        rest = b""
        remaining = length - 11
        try:
            while len(rest) < remaining:
                chunk = src.recv(remaining - len(rest))
                if not chunk:
                    return
                rest += chunk
        except OSError:
            return
        if flags & 0x80:
            err = struct.unpack(">H", hdr[9:11])[0]
            entry = f"{direction} REPLY  id={pid} err={err} data={rest.hex()}"
        else:
            entry = f"{direction} COMMAND id={pid} cmdset={hdr[9]} cmd={hdr[10]} data={rest.hex()}"
        log.append(entry)
        print(entry, flush=True)
        try:
            dst.sendall(hdr + rest)
        except OSError:
            return


def main():
    listen_port, host, port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", listen_port))
    server.listen(1)
    print(f"proxy listening on :{listen_port} -> {host}:{port}", flush=True)
    client, _ = server.accept()
    target = socket.create_connection((host, port))
    hs = client.recv(14)
    target.sendall(hs)
    hs2 = target.recv(14)
    client.sendall(hs2)
    log = []
    threading.Thread(target=pipe, args=(client, target, "C2S", log), daemon=True).start()
    threading.Thread(target=pipe, args=(target, client, "S2C", log), daemon=True).start()
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        cmd = line.strip()
        if cmd == "dump":
            for entry in log:
                print(entry, flush=True)
        elif cmd == "clear":
            log.clear()


if __name__ == "__main__":
    main()

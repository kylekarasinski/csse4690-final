"""MIM proxy (UDP only, minimal). Receives client datagrams, applies loss/delay/jitter,
forwards survivors to the server, logs every decision. Control commands arrive over TCP.

Run:  python mim.py --server 127.0.0.1:9001
"""
import argparse, asyncio, json, time
import chaos

settings = dict(chaos.DEFAULTS)    # the chaos engine defines which settings exist
stats = {"received": 0, "forwarded": 0, "dropped": 0}


class Relay(asyncio.DatagramProtocol):
    def __init__(self, server_addr, log):
        self.server_addr, self.log = server_addr, log

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, client_addr):
        stats["received"] += 1
        seq = json.loads(data).get("seq")          # only for logging; data is forwarded untouched
        entry = {"seq": seq, "t": time.time()}

        delay = chaos.decide(settings)             # chaos engine decides the fate
        if delay is None:
            stats["dropped"] += 1
            entry["action"] = "drop"
        else:
            # call_later = "send this later" without blocking new arrivals. Reordering falls out naturally.
            asyncio.get_running_loop().call_later(delay, self.forward, data)
            entry.update(action="forward", delay_ms=round(delay * 1000, 3))
        self.log.write(json.dumps(entry) + "\n")

    def forward(self, data):
        self.transport.sendto(data, self.server_addr)
        stats["forwarded"] += 1


async def handle_control(reader, writer):
    """One JSON command per line in, one JSON reply per line out."""
    while line := await reader.readline():
        try:
            cmd = json.loads(line)
            if cmd["cmd"] == "set":
                for k in settings:
                    if k in cmd:
                        settings[k] = float(cmd[k])
                reply = {"ok": True, "settings": settings}
            elif cmd["cmd"] == "stats":
                reply = {"ok": True, "stats": stats, "settings": settings}
            elif cmd["cmd"] == "ping":
                reply = {"ok": True, "pong": time.time()}
            else:
                reply = {"ok": False, "error": "unknown cmd"}
        except Exception as e:
            reply = {"ok": False, "error": str(e)}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
    writer.close()


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--data-port", type=int, default=9000)
    p.add_argument("--control-port", type=int, default=9003)
    p.add_argument("--server", default="127.0.0.1:9001")
    p.add_argument("--log", default="mim_log.jsonl")
    a = p.parse_args()
    host, port = a.server.split(":")

    loop = asyncio.get_running_loop()
    with open(a.log, "w", buffering=1) as log:
        await loop.create_datagram_endpoint(lambda: Relay((host, int(port)), log),
                                            local_addr=(a.listen, a.data_port))
        ctl = await asyncio.start_server(handle_control, a.listen, a.control_port)
        print(f"MIM: data UDP {a.data_port} -> {a.server}, control TCP {a.control_port}")
        async with ctl:
            await ctl.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

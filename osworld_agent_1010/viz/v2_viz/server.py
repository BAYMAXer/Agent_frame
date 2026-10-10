"""ComponentBench v2 SFT 数据集可视化服务器。

用法：python server.py  →  浏览器打开 http://127.0.0.1:8091
"""
import http.server
import json
import os
import socketserver
import urllib.parse

DATASET = r"C:\Users\c50064419\AppData\Roaming\WeLink_Desktop\appdata\IM\c50064419\DownloadFiles\v2"
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8091

MODES = ["bid", "coord"]
SPLITS = ["train", "val", "test"]


def load_trajectories(mode, split):
    path = os.path.join(DATASET, mode, split + ".jsonl")
    trajs = []
    if not os.path.exists(path):
        return trajs
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            images = rec.get("images", [])
            convs = rec.get("conversations", [])
            steps = []
            # conversations 是 human/gpt 交替：human(2i), gpt(2i+1)
            n = len(convs) // 2
            for i in range(n):
                human = convs[2 * i]["value"] if 2 * i < len(convs) else ""
                gpt = convs[2 * i + 1]["value"] if 2 * i + 1 < len(convs) else ""
                img = images[i] if i < len(images) else None
                steps.append({"image": img, "human": human, "action": gpt})
            trajs.append({
                "id": len(trajs),
                "task_id": rec.get("task_id"),
                "component": rec.get("component"),
                "library": rec.get("library"),
                "n_steps": len(steps),
                "steps": steps,
            })
    return trajs


# 启动时把 6 个文件全部载入内存（共约 24MB）
DATA = {}
for m in MODES:
    for s in SPLITS:
        DATA[(m, s)] = load_trajectories(m, s)
print("已加载:")
for (m, s), t in sorted(DATA.items()):
    print(f"  {m}/{s}: {len(t)} 条轨迹")


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)

        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return

        if path == "/api/list":
            mode = q.get("mode", ["bid"])[0]
            split = q.get("split", ["train"])[0]
            trajs = DATA.get((mode, split), [])
            summary = [{"id": t["id"], "task_id": t["task_id"], "component": t["component"],
                        "library": t["library"], "n_steps": t["n_steps"]} for t in trajs]
            self._send(200, json.dumps(summary, ensure_ascii=False), "application/json")
            return

        if path == "/api/traj":
            mode = q.get("mode", ["bid"])[0]
            split = q.get("split", ["train"])[0]
            try:
                tid = int(q.get("id", ["0"])[0])
            except ValueError:
                tid = 0
            trajs = DATA.get((mode, split), [])
            if 0 <= tid < len(trajs):
                self._send(200, json.dumps(trajs[tid], ensure_ascii=False), "application/json")
            else:
                self._send(404, "not found", "text/plain")
            return

        if path.startswith("/images/"):
            rel = urllib.parse.unquote(path[len("/images/"):])
            fp = os.path.normpath(os.path.join(DATASET, "images", rel))
            # 防目录穿越
            if os.path.commonprefix([fp, DATASET + os.sep]) != DATASET + os.sep:
                self._send(403, "forbidden", "text/plain")
                return
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, "not found", "text/plain")
            return

        self._send(404, "not found", "text/plain")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler)
    print(f"\n可视化地址: http://127.0.0.1:{PORT}\n")
    httpd.serve_forever()

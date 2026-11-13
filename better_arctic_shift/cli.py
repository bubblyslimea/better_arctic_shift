"""the command line door to your saved threads"""

import argparse
import json
import sys
import time

from .collector import Collector, Network, work_once
from .model import SOURCES, fullname
from .server import serve
from .store import Store
from .transfer import export_file, import_file


def main(argv=None):
    parser = argparse.ArgumentParser(description="keep a thread's text and the changes that follow")
    parser.add_argument("--db", default=".arctic/history.sqlite3", help="where your saved history lives")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="prepare an empty history store")
    capture = commands.add_parser("collect", help="capture a thread and check its saved comment ids")
    capture.add_argument("thread")
    capture.add_argument("--resume", help="continue a saved run id")
    capture.add_argument("--max-requests", type=int, default=10000)
    watch = commands.add_parser("watch", help="add a thread to the revisit queue")
    watch.add_argument("thread")
    watch.add_argument("--every", type=float, default=21600, help="seconds between visits")
    worker = commands.add_parser("work", help="process watched threads while this command is running")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll", type=float, default=10)
    load = commands.add_parser("import", help="bring in an archive or dated browser capture")
    load.add_argument("path")
    load.add_argument("--thread")
    load.add_argument("--source", choices=sorted(SOURCES), default="arctic_shift")
    load.add_argument("--observed-at")
    export = commands.add_parser("export", help="save a scrapbook JSON file or ZIP")
    export.add_argument("thread")
    export.add_argument("path")
    for name in ("coverage", "search", "history"):
        sub = commands.add_parser(name)
        sub.add_argument("thread")
        if name == "search":
            sub.add_argument("text", nargs="?", default="")
            sub.add_argument("--removed", action="store_true")
        if name == "history":
            sub.add_argument("entity")
    api = commands.add_parser("serve", help="open the local read-only API")
    api.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        result = None
        if args.command == "init":
            result = {"status": "ready"}
        elif args.command == "collect":
            if args.max_requests < 1:
                parser.error("choose a positive request budget")
            result = Collector(store, max_requests=args.max_requests).collect(args.thread, args.resume)
        elif args.command == "watch":
            store.watch(args.thread, args.every)
            result = {"status": "queued"}
        elif args.command == "work":
            if args.poll <= 0:
                parser.error("choose a positive polling interval")
            network = Network()
            while True:
                result = work_once(store, network)
                if result:
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                if args.once:
                    return 0
                if result is None:
                    time.sleep(min(args.poll, 60))
        elif args.command == "import":
            thread = import_file(store, args.path, args.thread, args.source, args.observed_at)
            result = store.coverage(thread)
        elif args.command == "export":
            export_file(store, args.thread, args.path)
            result = {"status": "saved"}
        elif args.command == "coverage":
            result = store.coverage(args.thread)
        elif args.command == "search":
            result = store.search(args.thread, args.text, args.removed)
        elif args.command == "history":
            result = store.entities(args.thread).get(fullname(args.entity))
        elif args.command == "serve":
            serve(args.db, args.port)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get("state") == "partial" else 0
    except (OSError, ValueError, KeyError) as error:
        print(f"couldn't finish: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("paused, your saved captures are still here", file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""walk archive files, with a small hook for your own record processing"""

import argparse
from pathlib import Path

try:
    from .fileStreams import getFileJsonStream
    from .utils import FileProgressLog
except ImportError:
    from fileStreams import getFileJsonStream
    from utils import FileProgressLog


fileOrFolderPath = r"<path to file or folder>"
recursive = False


def processFile(path: str, on_record=None):
    with open(path, "rb") as stream:
        records = getFileJsonStream(path, stream)
        if records is None:
            return 0
        progress = FileProgressLog(path, stream)
        for row in records:
            if on_record:
                on_record(row)
            # add your own processing here, or pass on_record
            progress.onRow()
        progress.logProgress("\n")
        return progress.i


def processFolder(path: str, on_record=None):
    root = Path(path)
    files = root.rglob("*") if recursive else root.iterdir()
    return sum(processFile(str(file), on_record) for file in sorted(files) if file.is_file())


def main():
    global recursive
    parser = argparse.ArgumentParser(description="read a file or folder of archive records")
    parser.add_argument("path", nargs="?", default=fileOrFolderPath)
    parser.add_argument("--recursive", action="store_true", default=recursive)
    args = parser.parse_args()
    recursive = args.recursive
    if args.path == "<path to file or folder>":
        parser.error("choose an archive file or folder")
    count = processFolder(args.path) if Path(args.path).is_dir() else processFile(args.path)
    print(f"finished reading {count:,} records")


if __name__ == "__main__":
    main()

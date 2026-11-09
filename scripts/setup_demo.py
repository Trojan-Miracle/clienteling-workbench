"""Initialize the fictional demo without overwriting an existing workspace."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / 'artifacts/v4/atelier.sqlite3'
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (ROOT / 'demo/atelier.sqlite3').open('rb') as source, target.open('xb') as output:
            shutil.copyfileobj(source, output)
    except FileExistsError:
        print('已有工作数据，保持不变。')
    else:
        print('已准备8位演示客户及回复建议。')


if __name__ == '__main__':
    main()

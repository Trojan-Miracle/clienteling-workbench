"""Local developer review + the real preference editor, with automatic advice preparation."""
import argparse
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from atelier.app import create_app
from atelier.engine import Engine
from atelier.store import RelationshipStore
from atelier.quick_review import register_quick_review


def make_app(store_path=None, review_path=None, provider=None, automation=False, recover=False):
    engine=Engine(RelationshipStore(store_path))
    app=create_app(engine,seed=False,automation=automation,recover=recover)
    register_quick_review(app,ROOT,review_path,provider)
    return app


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8791)
    parser.add_argument('--retrieval',choices=['lexical','hybrid'],default='lexical')
    args=parser.parse_args()
    os.environ['LANXU_RETRIEVAL_MODE']=args.retrieval
    make_app(automation=True,recover=True).run(host='127.0.0.1',port=args.port,debug=False,threaded=True,load_dotenv=False)

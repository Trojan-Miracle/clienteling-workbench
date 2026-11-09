"""Download the MIT-licensed Chinese embedding model before serving requests."""
from pathlib import Path
from huggingface_hub import snapshot_download

if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]/'.runtime/bge-small-zh-v1.5'
    snapshot_download('Qdrant/bge-small-zh-v1.5',revision='46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59',local_dir=root,
                      allow_patterns=['*.json','*.txt','*.onnx','README.md'])
    print('中文检索模型已保存：',root)

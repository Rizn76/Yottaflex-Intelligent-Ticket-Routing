from src.generator import generate
from src.validate import validate

def test_small_dataset(tmp_path):
    out=tmp_path/"generated"
    generate(seed=99,tickets=100,agents=10,messages=250,event_days=7,output_dir=str(out))
    assert validate(str(out))

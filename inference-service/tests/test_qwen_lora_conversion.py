import json
from pathlib import Path
import struct
import importlib.util

spec = importlib.util.spec_from_file_location("prepare_dgx_dev", Path(__file__).parents[1] / "scripts/prepare_dgx_dev.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
convert_qwen_lora = module.convert_qwen_lora


def test_fused_lora_preserves_gate_and_up_matrix_products(tmp_path):
    prefix = "diffusion_model.transformer_blocks.0.img_mlp."
    tensors = {
        prefix + "gate_up.lora_A.weight": ([1, 2], [2., 3.]),
        prefix + "gate_up.lora_B.weight": ([4, 1], [5., 7., 11., 13.]),
        prefix + "out.lora_A.weight": ([1, 2], [17., 19.]),
    }
    header, payload = {}, b""
    for key, (shape, values) in tensors.items():
        data = struct.pack("<" + "f" * len(values), *values)
        header[key] = {"dtype": "F32", "shape": shape, "data_offsets": [len(payload), len(payload) + len(data)]}
        payload += data
    encoded = json.dumps(header).encode()
    source, target = tmp_path / "source.safetensors", tmp_path / "converted.safetensors"
    source.write_bytes(struct.pack("<Q", len(encoded)) + encoded + payload)
    original = source.read_bytes()
    assert convert_qwen_lora(source, target)
    raw = target.read_bytes()
    size = struct.unpack("<Q", raw[:8])[0]
    converted = json.loads(raw[8:8 + size])

    def values(key):
        begin, end = converted[key]["data_offsets"]
        data = raw[8 + size + begin:8 + size + end]
        return struct.unpack("<" + "f" * (len(data) // 4), data)

    # Applying either split projection produces exactly the corresponding
    # half of the original fused delta B @ A @ x.
    x = [23., 29.]
    fused_delta = [b * (2 * x[0] + 3 * x[1]) for b in [5., 7., 11., 13.]]
    for projection, expected in [("gate_layer", fused_delta[:2]), ("proj", fused_delta[2:])]:
        a = values(prefix + projection + ".lora_A.weight")
        b = values(prefix + projection + ".lora_B.weight")
        assert [v * sum(i * j for i, j in zip(a, x)) for v in b] == expected
    assert values(prefix + "out.lora_A.weight") == (17., 19.)
    assert not any("gate_up" in key for key in converted)
    assert source.read_bytes() == original
    assert convert_qwen_lora(source, target)
    assert target.read_bytes() == raw

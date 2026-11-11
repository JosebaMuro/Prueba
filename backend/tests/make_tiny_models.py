"""Crea modelos diminutos (pesos aleatorios, ~1 MB) en los formatos de ONNX Runtime GenAI y OpenVINO GenAI
para probar los motores de verdad sin descargar nada de Hugging Face.

Requisitos (solo para generarlos): torch, transformers, tokenizers, onnx, onnxruntime-genai, optimum-intel[openvino]
Uso:  python backend/tests/make_tiny_models.py [carpeta]      (por defecto backend/tests/.tiny)
Después: python backend/tests/test_engines.py
"""
import subprocess
import sys
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / ".tiny").resolve()
HF = OUT / "hf"

TEMPLATE = (
    "{% if tools %}<|im_start|>system\n{% if messages[0].role == 'system' %}{{ messages[0].content }}\n\n{% endif %}"
    "# Tools\n<tools>\n{% for t in tools %}{{ t | tojson }}\n{% endfor %}</tools>\n"
    "Llama a una función con <tool_call>{\"name\": ..., \"arguments\": ...}</tool_call><|im_end|>\n"
    "{% elif messages[0].role == 'system' %}<|im_start|>system\n{{ messages[0].content }}<|im_end|>\n{% endif %}"
    "{% for m in messages %}{% if m.role == 'system' %}{% elif m.role == 'tool' %}"
    "<|im_start|>user\n<tool_response>\n{{ m.content }}\n</tool_response><|im_end|>\n"
    "{% elif m.tool_calls %}<|im_start|>assistant\n{% for c in m.tool_calls %}<tool_call>\n"
    "{\"name\": \"{{ c.function.name }}\", \"arguments\": {{ c.function.arguments | tojson }}}\n</tool_call>{% endfor %}<|im_end|>\n"
    "{% else %}<|im_start|>{{ m.role }}\n{{ m.content }}<|im_end|>\n{% endif %}{% endfor %}"
    "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}")


def make_hf():
    import torch
    from tokenizers import Tokenizer, models, pre_tokenizers, decoders, trainers
    from transformers import PreTrainedTokenizerFast, Qwen2Config, Qwen2ForCausalLM

    HF.mkdir(parents=True, exist_ok=True)
    special = ["<|endoftext|>", "<|im_start|>", "<|im_end|>"]
    tok = Tokenizer(models.BPE(unk_token=None))
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    corpus = ["Hola, ¿qué tal? El resultado es 42. Eres un asistente útil.",
              '<tool_call>{"name": "run_python", "arguments": {"code": "6*7"}}</tool_call>',
              "The quick brown fox jumps over the lazy dog 0123456789"] * 50
    tok.train_from_iterator(corpus, trainers.BpeTrainer(vocab_size=600, special_tokens=special,
                                                        initial_alphabet=pre_tokenizers.ByteLevel.alphabet()))
    fast = PreTrainedTokenizerFast(tokenizer_object=tok, eos_token="<|im_end|>", pad_token="<|endoftext|>",
                                   bos_token=None, additional_special_tokens=special)
    fast.chat_template = TEMPLATE
    fast.save_pretrained(HF)
    cfg = Qwen2Config(vocab_size=len(fast), hidden_size=64, intermediate_size=128, num_hidden_layers=2,
                      num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=4096,
                      eos_token_id=fast.convert_tokens_to_ids("<|im_end|>"),
                      pad_token_id=fast.convert_tokens_to_ids("<|endoftext|>"), tie_word_embeddings=True)
    torch.manual_seed(0)
    Qwen2ForCausalLM(cfg).to(torch.float32).save_pretrained(HF)
    (HF / "generation_config.json").write_text('{"eos_token_id": %d}' % cfg.eos_token_id)


def run(cmd):
    print("$", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    make_hf()
    run([sys.executable, "-m", "onnxruntime_genai.models.builder", "-i", str(HF), "-m", str(HF),
         "-o", str(OUT / "onnx"), "-p", "fp32", "-e", "cpu", "-c", str(OUT / ".cache")])
    for f in ("tokenizer_config.json",):  # la plantilla con herramientas viaja con el modelo
        (OUT / "onnx" / f).write_text((HF / f).read_text())
    run([sys.executable, "-m", "optimum.commands.optimum_cli", "export", "openvino", "-m", str(HF),
         "--task", "text-generation-with-past", "--weight-format", "fp32", str(OUT / "openvino")])
    print("Modelos diminutos en", OUT)

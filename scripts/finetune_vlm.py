"""Single-GPU Qwen2.5-VL LoRA/QLoRA SFT for JSONL image-document examples.

Each JSONL record has `images` (one or more paths, relative to that JSONL file),
`prompt`, and `answer` (target JSON string). Keep benchmark documents out of training.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path


def read_records(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for number, row in enumerate(rows, 1):
        if not isinstance(row.get("answer"), str) or not row["answer"].strip():
            raise ValueError(f"{path}:{number} must contain a nonempty string `answer`")
        row["images"] = row.get("images") or ([row["image"]] if row.get("image") else [])
        if not row["images"]:
            raise ValueError(f"{path}:{number} must include `image` or `images`")
        row["images"] = [str((path.parent / item).resolve()) for item in row["images"]]
        for image in row["images"]:
            if not Path(image).is_file():
                raise FileNotFoundError(f"Image does not exist: {image}")
        row.setdefault("prompt", "Extract the document into the requested JSON schema. Do not guess missing values; include short evidence quotes.")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-jsonl", type=Path, required=True)
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--model", default="Qwen/Qwen2.5-VL-3B-Instruct")
    parser.add_argument("--output", type=Path, default=Path("models/document-vlm-lora"))
    parser.add_argument("--qlora", action="store_true", help="Load the base model in 4-bit NF4; requires CUDA and bitsandbytes")
    parser.add_argument("--epochs", type=float, default=3)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--max-pixels", type=int, default=786432)
    parser.add_argument("--max-steps", type=int, default=-1)
    args = parser.parse_args()

    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoProcessor, BitsAndBytesConfig, Qwen2_5_VLForConditionalGeneration, Trainer, TrainingArguments
    from qwen_vl_utils import process_vision_info

    if not torch.cuda.is_available():
        raise SystemExit("LoRA/QLoRA training requires a CUDA GPU for this script. Inference can still run locally without a GPU.")
    train_rows = read_records(args.train_jsonl.resolve())
    validation_rows = read_records(args.validation_jsonl.resolve())
    args.output.mkdir(parents=True, exist_ok=True)
    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()

    processor = AutoProcessor.from_pretrained(args.model, max_pixels=args.max_pixels)
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    load_args = {"torch_dtype": dtype, "device_map": "auto", "trust_remote_code": False}
    if args.qlora:
        load_args["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=dtype,
        )
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(args.model, **load_args)
    if args.qlora:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    adapter = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(model, adapter)
    model.config.use_cache = False
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()
    model.print_trainable_parameters()
    trainable = sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)

    class DocumentDataset(torch.utils.data.Dataset):
        def __init__(self, records):
            self.records = records

        def __len__(self):
            return len(self.records)

        def __getitem__(self, index):
            return self.records[index]

    def collate(batch):
        # Per-example collation preserves variable image counts and resolutions.
        row = batch[0]
        user_content = [{"type": "image", "image": image} for image in row["images"]]
        user_content.append({"type": "text", "text": row["prompt"]})
        prompt_messages = [{"role": "user", "content": user_content}]
        full_messages = prompt_messages + [{"role": "assistant", "content": [{"type": "text", "text": row["answer"]}]}]
        prompt_text = processor.apply_chat_template(prompt_messages, tokenize=False, add_generation_prompt=True)
        full_text = processor.apply_chat_template(full_messages, tokenize=False, add_generation_prompt=False)
        image_inputs, video_inputs = process_vision_info(full_messages)
        prompt_inputs = processor(text=[prompt_text], images=image_inputs, videos=video_inputs, return_tensors="pt")
        full_inputs = processor(text=[full_text], images=image_inputs, videos=video_inputs, padding=True, return_tensors="pt")
        labels = full_inputs["input_ids"].clone()
        prompt_length = min(prompt_inputs["input_ids"].shape[1], labels.shape[1])
        labels[:, :prompt_length] = -100
        labels[full_inputs["attention_mask"] == 0] = -100
        full_inputs["labels"] = labels
        return full_inputs

    training_args = TrainingArguments(
        output_dir=str(args.output),
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        learning_rate=args.learning_rate,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=8,
        gradient_checkpointing=True,
        optim="paged_adamw_8bit" if args.qlora else "adamw_torch",
        logging_steps=5,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        bf16=dtype == torch.bfloat16,
        fp16=dtype == torch.float16,
        remove_unused_columns=False,
        report_to="none",
        dataloader_num_workers=0,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=DocumentDataset(train_rows),
        eval_dataset=DocumentDataset(validation_rows),
        data_collator=collate,
        processing_class=processor,
    )
    trainer.train()
    trainer.save_model(str(args.output))
    processor.save_pretrained(args.output)
    metadata = {
        "base_model": args.model,
        "method": "QLoRA-4bit-NF4" if args.qlora else "LoRA",
        "train_examples": len(train_rows),
        "validation_examples": len(validation_rows),
        "trainable_parameters": trainable,
        "trainable_parameter_fraction": trainable / sum(parameter.numel() for parameter in model.parameters()),
        "learning_rate": args.learning_rate,
        "epochs_requested": args.epochs,
        "resolution_max_pixels": args.max_pixels,
        "training_seconds": round(time.perf_counter() - started, 2),
        "peak_cuda_memory_bytes": torch.cuda.max_memory_allocated(),
        "peak_cuda_reserved_bytes": torch.cuda.max_memory_reserved(),
        "gpu_name": torch.cuda.get_device_name(0),
        "train_manifest_sha256": hashlib.sha256(args.train_jsonl.read_bytes()).hexdigest(),
        "validation_manifest_sha256": hashlib.sha256(args.validation_jsonl.read_bytes()).hexdigest(),
        "validation_metrics": next((item for item in reversed(trainer.state.log_history) if "eval_loss" in item), {}),
    }
    (args.output / "run_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()

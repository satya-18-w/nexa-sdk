import argparse
import base64
import glob
import logging
import os
import readline
import sys
import time
from pathlib import Path
from typing import Iterator

from streamlit.web import cli as stcli

from nexa.constants import (
    DEFAULT_TEXT_GEN_PARAMS,
    NEXA_RUN_CHAT_TEMPLATE_MAP,
    NEXA_RUN_MODEL_MAP_VLM,
    NEXA_RUN_PROJECTOR_MAP,
)
from nexa.general import pull_model
from llama_cpp import llama
from nexa.gguf.llama.llama_chat_format import (
    Llava15ChatHandler,
    Llava16ChatHandler,
    NanoLlavaChatHandler,
)
from nexa.utils import SpinningCursorAnimation, nexa_prompt, suppress_stdout_stderr

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def _complete(text, state):
    return (glob.glob(text + "*") + [None])[state]


def image_to_base64_data_uri(file_path):
    if file_path and os.path.exists(file_path):
        with open(file_path, "rb") as img_file:
            base64_data = base64.b64encode(img_file.read()).decode("utf-8")
            return f"data:image/png;base64,{base64_data}"
    return None


# HACK: This is moved from nexa.constants to avoid circular imports
NEXA_PROJECTOR_HANDLER_MAP: dict[str, Llava15ChatHandler] = {
    "nanollava": NanoLlavaChatHandler,
    "llava-phi3": Llava15ChatHandler,
    # "llava1.5": Llava15ChatHandler,
    "llava-llama3": Llava15ChatHandler,
    "llava1.6-mistral": Llava16ChatHandler,
    "llava1.6-vicuna": Llava16ChatHandler,
}

assert (
    set(NEXA_RUN_MODEL_MAP_VLM.keys())
    == set(NEXA_RUN_PROJECTOR_MAP.keys())
    == set(NEXA_PROJECTOR_HANDLER_MAP.keys())
), "Model, projector, and handler should have the same keys"


class NexaVLMInference:
    """
    A class used for loading VLM models and running text generation.

    Methods:
    run: Run the text generation loop.
    run_streamlit: Run the Streamlit UI.

    Args:
    model_path (str): Path or identifier for the model in Nexa Model Hub.
    stop_words (list): List of stop words for early stopping.
    profiling (bool): Enable timing measurements for the generation process.
    streamlit (bool): Run the inference in Streamlit UI.
    temperature (float): Temperature for sampling.
    max_new_tokens (int): Maximum number of new tokens to generate.
    top_k (int): Top-k sampling parameter.
    top_p (float): Top-p sampling parameter
    """
    def __init__(self, model_path, stop_words=None, **kwargs):
        self.params = DEFAULT_TEXT_GEN_PARAMS
        self.params.update(kwargs)
        self.model = None
        self.projector = None

        self.projector_path = NEXA_RUN_PROJECTOR_MAP.get(model_path, None)
        self.downloaded_path = None
        self.projector_downloaded_path = None

        if model_path in NEXA_RUN_MODEL_MAP_VLM:
            logging.debug(f"Found model {model_path} in public hub")
            self.model_path = NEXA_RUN_MODEL_MAP_VLM.get(model_path)
            self.projector_path = NEXA_RUN_PROJECTOR_MAP.get(model_path)
            self.downloaded_path = pull_model(self.model_path)
            self.projector_downloaded_path = pull_model(self.projector_path)
        elif (local_dir := Path(model_path).parent).exists():
            logging.debug(f"Using local model at {local_dir}")
            model_name = Path(model_path).name
            tag_and_ext = model_name.split(":")[-1]
            self.downloaded_path = local_dir / f"model-{tag_and_ext}"
            self.projector_downloaded_path = local_dir / f"projector-{tag_and_ext}"
            if not (
                self.downloaded_path.exists()
                and self.projector_downloaded_path.exists()
            ):
                logging.error(
                    f"Model or projector not found in {local_dir}. "
                    "Make sure to name them as 'model-<tag>.gguf' and 'projector-<tag>.gguf'."
                )
                exit(1)
        else:
            logging.error("Using model from hub is not supported yet.")
            exit(1)

        if self.downloaded_path is None:
            logging.error(
                f"Model ({model_path}) is not appicable. Please refer to our docs for proper usage.",
                exc_info=True,
            )
            exit(1)

        self.projector_handler = NEXA_PROJECTOR_HANDLER_MAP.get(
            model_path, Llava15ChatHandler
        )
        self.stop_words = stop_words if stop_words else []
        self.profiling = kwargs.get("profiling", False)

        self.chat_format = NEXA_RUN_CHAT_TEMPLATE_MAP.get(model_path, None)

        if not kwargs.get("streamlit", False):
            self._load_model()
            if self.model is None:
                logging.error(
                    "Failed to load model or tokenizer. Exiting.", exc_info=True
                )
                exit(1)

    @SpinningCursorAnimation()
    def _load_model(self):
        logging.debug(f"Loading model from {self.downloaded_path}")
        start_time = time.time()
        with suppress_stdout_stderr():
            self.projector = (
                self.projector_handler(
                    clip_model_path=self.projector_downloaded_path, verbose=False
                )
                if self.projector_downloaded_path
                else None
            )
            self.model = llama.Llama(
                model_path=self.downloaded_path,
                chat_handler=self.projector,
                verbose=False,
                chat_format=self.chat_format,
                n_ctx=self.params.get("max_new_tokens", 2048),
                n_gpu_layers=-1,  # offload all layers to GPU
            )
        load_time = time.time() - start_time
        if self.profiling:
            logging.info(f"Model loaded in {load_time:.2f} seconds")

    def run(self):
        # I just use completion, no conversation history
        while True:
            try:
                generated_text = ""
                readline.set_completer_delims(" \t\n;")
                readline.parse_and_bind("tab: complete")
                readline.set_completer(_complete)

                image_path = nexa_prompt("Image Path (leave empty if no image)")
                if image_path and not os.path.exists(image_path):
                    print(f"'{image_path}' is not a path to image. Will ignore.")

                user_input = nexa_prompt()

                if not user_input and not image_path:
                    print("Please provide an image or text input.")
                    continue

                output = self._chat(user_input, image_path)
                for chunk in output:
                    delta = chunk["choices"][0]["delta"]
                    if "role" in delta:
                        print(delta["role"], end=": ", flush=True)
                    elif "content" in delta:
                        print(delta["content"], end="", flush=True)
                        generated_text += delta["content"]
            except KeyboardInterrupt:
                pass
            except Exception as e:
                logging.error(f"Error during generation: {e}", exc_info=True)
            print("\n")

    def _chat(self, user_input: str, image_path: str = None) -> Iterator:
        data_uri = image_to_base64_data_uri(image_path) if image_path else None

        content = [{"type": "text", "text": user_input}]
        if data_uri:
            content.insert(0, {"type": "image_url", "image_url": {"url": data_uri}})

        messages = [
            {
                "role": "system",
                "content": "You are an assistant who perfectly describes images.",
            },
            {
                "role": "user",
                "content": content,
            },
        ]

        return self.model.create_chat_completion(
            messages=messages,
            temperature=self.params["temperature"],
            max_tokens=self.params["max_new_tokens"],
            top_k=self.params["top_k"],
            top_p=self.params["top_p"],
            stream=True,
            stop=self.stop_words,
        )

    def run_streamlit(self, model_path: str):
        logging.info("Running Streamlit UI...")

        streamlit_script_path = (
            Path(os.path.abspath(__file__)).parent / "streamlit" / "streamlit_vlm.py"
        )

        sys.argv = ["streamlit", "run", str(streamlit_script_path), model_path]
        sys.exit(stcli.main())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run VLM inference with a specified model"
    )
    parser.add_argument(
        "model_path",
        type=str,
        help="Path or identifier for the model in Nexa Model Hub",
    )
    parser.add_argument(
        "-t", "--temperature", type=float, default=0.8, help="Temperature for sampling"
    )
    parser.add_argument(
        "-m",
        "--max_new_tokens",
        type=int,
        default=2048,
        help="Maximum number of new tokens to generate",
    )
    parser.add_argument(
        "-k", "--top_k", type=int, default=50, help="Top-k sampling parameter"
    )
    parser.add_argument(
        "-p", "--top_p", type=float, default=1.0, help="Top-p sampling parameter"
    )
    parser.add_argument(
        "-sw",
        "--stop_words",
        nargs="*",
        default=[],
        help="List of stop words for early stopping",
    )
    parser.add_argument(
        "-pf",
        "--profiling",
        action="store_true",
        help="Enable timing measurements for the generation process",
    )
    parser.add_argument(
        "-st",
        "--streamlit",
        action="store_true",
        help="Run the inference in Streamlit UI",
    )
    args = parser.parse_args()
    kwargs = {k: v for k, v in vars(args).items() if v is not None}
    model_path = kwargs.pop("model_path")
    stop_words = kwargs.pop("stop_words", [])
    inference = NexaVLMInference(model_path, stop_words=stop_words, **kwargs)
    if args.streamlit:
        inference.run_streamlit(model_path)
    else:
        inference.run()

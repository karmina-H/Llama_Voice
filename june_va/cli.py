"""
CLI Application for Text-to-Speech (TTS) and Speech-to-Text (STT) integration with Language Learning Models (LLM).

This module uses asyncio for asynchronous operations, threading for parallel task execution, and various
third-party libraries for audio processing and command-line interaction.
"""

import asyncio
import logging
import os.path
import re
import time
from json import loads
from threading import Thread
from typing import Optional

import click
import pygame.mixer
from colorama import Fore, Style, init

from . import __version__
from .audio import AudioIO
from .models import LLM, STT, TTS
from .settings import default_config
from .utils import ThreadSafeState, deep_merge_dicts, logger, print_system_message

logging.getLogger("TTS").setLevel(logging.ERROR)
pygame.mixer.init()


class AppState:
    """Enumeration for application states."""

    READY_FOR_INPUT = 0  # 0 = 사용자로부터 입력 대기상태
    LLM_RESPONSE_GENERATED = 1 # 1 = 응답 생성 완료


current_app_state = ThreadSafeState(AppState.READY_FOR_INPUT) # 현재 LLM상태 추적(입력가능한지 아님 LLM이 응답 생성중인지)
tts_generation_error = ThreadSafeState(False) # TTS 실패 여부 플래그
shutdown_event = asyncio.Event() # 비동기 루프 종료 신호

# 큐 안에있는 작업 모두종료 하는 함수
async def _clear_queue(queue: asyncio.Queue[str]):
    """
    Clear all items from the asyncio queue.

    Args:
        queue: The queue to be cleared.
    """
    while not queue.empty():
        _ = await queue.get()
        queue.task_done()

# 프로그램 진입접
async def _real_main(**kwargs):
    """
    Main function to set up models, process configurations, and handle producer-consumer tasks.

    Args:
        **kwargs: Arbitrary keyword arguments including config file.
    """
    user_config = loads(kwargs["config"].read()) if kwargs["config"] else {}
    config = deep_merge_dicts(default_config, user_config)

    llm_config = config["llm"]
    stt_config = config.get("stt") or {}
    tts_config = config.get("tts") or {}
    
    #pyaudio설치되어있는지 체크
    if stt_config:
        try:
            import pyaudio
        except ImportError:
            print_system_message(
                (
                    "PyAudio not installed. Please install PyAudio for speech recognition and audio synthesis to "
                    "work."
                ),
                color=Fore.RED,
                log_level=logging.ERROR,
            )
            return 1

    llm_model = LLM(**llm_config)
    # LLM모델 유효성 확인하는 부분
    if not llm_model.exists():
        print_system_message(f"Invalid ollama model: {llm_model.model_id}", color=Fore.RED, log_level=logging.ERROR)
        return 2

    if llm_config.get("disable_chat_history"): #disable_chat_history이게 true로 설정되어있다면 이전 대화 기록을 기록하지않고 매 질문마다 독립적으로 처리하게함.
        print_system_message(
            "Chat history is currently disabled. The conversation may not be fully interactive, as the "
            "assistant will not retain previous context. Each interaction will be treated independently.",
            color=Fore.YELLOW,
        )

    if not llm_config.get("system_prompt"): # system prompt는 LLM에게 기본 역할이나 대화 분위기를 정해주는 안내 문장
        print_system_message("No system prompt provided.")

    # STT, TTS모델 인스턴스 초기화화
    stt_model = STT(**stt_config) if stt_config else None
    tts_model = TTS(**tts_config) if tts_config else None

    #llm이 생성한 응답을 저장해두는 비동기 큐로, 오디오 생성에 사용됨.
    text_queue = asyncio.Queue()

    # run_async_tasks이 함수를 새로운 스레드에서 실행. 이 함수run_async_tasks는 음성 생성및 재생을 실행하는 역할을함.
    thread = Thread(target=run_async_tasks, args=(text_queue, tts_model))
    thread.start()

    try:
        producer(text_queue, llm_model, stt_model)
    except KeyboardInterrupt:
        ...
    finally:
        #producer함수가 실행되고 남은 데이터 처리할때까지 기다리는 부분
        shutdown_event.set()
        thread.join()
        await _clear_queue(text_queue)
        await text_queue.join()
        
        if tts_model and os.path.exists(tts_model.file_path):
            os.remove(tts_model.file_path)


async def consumer(text_queue: asyncio.Queue[str], tts_model: Optional[TTS]):
    """
    Consumer task to process text from the queue and generate TTS output.

    Args:
        text_queue: Queue containing text to process.
        tts_model: Text-to-Speech model for generating audio.
    """
    with AudioIO() as audio_io:
        while not shutdown_event.is_set():
            try:
                synthesis = None
                text_buffer = text_queue.get_nowait()

                if tts_model:
                    try:
                        synthesis = tts_model.forward(text_buffer)
                    except:
                        tts_generation_error.set_value(True)

                if synthesis:
                    while pygame.mixer.music.get_busy():
                        await asyncio.sleep(0.25)

                    tts_model.model.synthesizer.save_wav(wav=synthesis, path=tts_model.file_path)

                    audio_io.play_wav(tts_model.file_path)

                text_queue.task_done()
            except asyncio.QueueEmpty:
                if current_app_state.get_value() != AppState.READY_FOR_INPUT:
                    # Wait for the last chunk of speech to be played fully
                    while pygame.mixer.music.get_busy():
                        await asyncio.sleep(0.25)

                    current_app_state.set_value(AppState.READY_FOR_INPUT)

                await asyncio.sleep(0.25)


async def start_async_tasks(text_queue: asyncio.Queue[str], tts_model: Optional[TTS]):
    """
    Start consumer task for processing text queue.

    Args:
        text_queue: Queue containing text to process.
        tts_model: Text-to-Speech model for generating audio.
    """
    consumer_task = asyncio.create_task(consumer(text_queue, tts_model))

    try:
        # Wait until consumer finishes
        await consumer_task
    except asyncio.CancelledError:
        ...


@click.command()
@click.option(
    "-c",
    "--config",
    help="Configuration file.",
    nargs=1,
    required=False,
    type=click.File("r", encoding="utf-8"),
)
@click.option(
    "-v",
    "--verbose",
    help="Verbose mode.",
    is_flag=True,
)
@click.version_option(__version__)
def main(**kwargs):
    """
    Local voice assistant tool.
    """
    if kwargs["verbose"]:
        logger.setLevel(logging.DEBUG)

    asyncio.run(_real_main(**kwargs))

# 사용자로부터 입력받는 함수
def producer(text_queue: asyncio.Queue[str], llm_model: LLM, stt_model: Optional[STT]) -> None:
    """
    Producer task to gather user input, process with LLM, and queue for TTS.

    Args:
        text_queue: Queue to put processed text chunks.
        llm_model: Language Learning Model for processing user input.
        stt_model: Speech-to-Text model for transcribing audio input.
    """
    audio_io = AudioIO()
    min_chunk_size = 10
    splitters = [".", ",", "?", ":", ";"]

    def get_user_input():
        if stt_model:
            audio_data = audio_io.record_audio()

            if audio_data is not None:
                print_system_message("Transcribing audio...")

                transcription = stt_model.forward(audio_data)

                return transcription

        return input(f"{Style.BRIGHT}{Fore.CYAN}[user]>{Style.RESET_ALL} ")

    # Regular expression pattern to match 'quit', 'stop', or 'exit', ignoring case
    exit_pattern = re.compile(r"\b(exit|quit|stop)\b", re.IGNORECASE)

    while True:
        if current_app_state.get_value() != AppState.READY_FOR_INPUT:
            time.sleep(0.25)
            continue

        if tts_generation_error.get_value():
            print_system_message(
                "Some text-to-speech generation failed.",
                color=Fore.YELLOW,
                log_level=logging.WARNING,
            )
            tts_generation_error.set_value(False)

        buffer = []
        user_input = get_user_input()

        if stt_model:
            print(f"{Style.BRIGHT}{Fore.CYAN}[user]>{Style.RESET_ALL} {user_input}")

        if user_input:
            if exit_pattern.search(user_input):
                print_system_message("Exiting...")
                break

            print(f"{Style.BRIGHT}{Fore.GREEN}[assistant]> {Style.NORMAL}", end="", flush=True)

            for token in llm_model.forward(user_input):
                print(token, end="", flush=True)

                buffer.append(token)

                # Check if buffer is ready to be chunked
                if token == "\n" or (len(buffer) >= min_chunk_size and token in splitters):
                    chunk = "".join(buffer).strip()

                    buffer.clear()

                    if chunk:
                        # Queue this chunk for TTS processing
                        text_queue.put_nowait(chunk)

            # Process any remaining text in buffer
            if buffer:
                chunk = "".join(buffer).strip()

                if chunk:
                    text_queue.put_nowait(chunk)

            current_app_state.set_value(AppState.LLM_RESPONSE_GENERATED)

            print(Style.RESET_ALL)

    audio_io.close()


def run_async_tasks(text_queue: asyncio.Queue[str], tts_model: Optional[TTS]):
    """
    Run async tasks in a new event loop for thread safety.

    Args:
        text_queue: Queue to put processed text chunks.
        tts_model: Text-to-Speech model for generating audio.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(start_async_tasks(text_queue, tts_model))
    except Exception:
        loop.close()

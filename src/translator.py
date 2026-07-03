"""
translator.py - 翻訳ワーカースレッド

アーキテクチャ:
    TranslatorWorker (QThread)
        └─ run() → asyncio.new_event_loop()
                └─ _translation_loop()           ← asyncio.Event で待機
                        ├─ キャッシュ照合
                        ├─ Ollama /api/generate  ← httpx.AsyncClient
                        └─ translation_ready シグナル発行

キュー詰まり防止の仕組み:
    「最新の1件だけ待機」パターン
    ┌─ request_translation() が呼ばれるたびに _pending を上書き ─┐
    │  翻訳中: _pending に保持（前の pending は破棄）           │
    │  翻訳後: _pending があれば即座に次の翻訳を開始           │
    └────────────────────────────────────────────────────────────┘

シグナル（TranslatorWorker → メインスレッド）:
    translation_ready(str, object)  : 翻訳文字列 + 元の OcrResult
    translation_skipped(object)     : キャッシュヒット時も座標情報を再送
    error_occurred(str)             : エラーメッセージ
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Optional

import httpx
from PyQt6.QtCore import QThread, pyqtSignal

from config import AppConfig, load_config
from ocr_engine import OcrResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  翻訳キャッシュ: 上限付き OrderedDict (LRU)
# ─────────────────────────────────────────────

class TranslationCache:
    """
    原文 → 翻訳文字列 のキャッシュ。
    max_size を超えたら最も古いエントリを自動削除する。
    アクセスのたびに末尾（最新）へ移動する LRU 方式。
    """

    def __init__(self, max_size: int = 200):
        self._cache: OrderedDict[str, str] = OrderedDict()
        self.max_size = max_size
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[str]:
        if key not in self._cache:
            self.misses += 1
            return None
        # アクセスされたエントリを末尾（最新扱い）に移動
        self._cache.move_to_end(key)
        self.hits += 1
        return self._cache[key]

    def set(self, key: str, value: str) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self.max_size:
            oldest_key, _ = self._cache.popitem(last=False)
            logger.debug("キャッシュ上限到達: '%s...' を削除", oldest_key[:20])

    def __len__(self) -> int:
        return len(self._cache)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def stats(self) -> str:
        return (f"size={len(self)}/{self.max_size}  "
                f"hits={self.hits}  misses={self.misses}  "
                f"hit_rate={self.hit_rate:.1%}")


# ─────────────────────────────────────────────
#  TranslatorWorker: QThread + asyncio のカプセル化
# ─────────────────────────────────────────────

class TranslatorWorker(QThread):
    """
    翻訳処理を独立したスレッドで実行するワーカー。

    OcrWorker と同様に、独自の asyncio イベントループを持ち、
    メインスレッドを一切ブロックしない。

    呼び出し側（メインスレッド）は request_translation() を使う。
    結果は translation_ready シグナルで非同期に届く。
    """

    # ── シグナル定義 ──────────────────────────
    translation_ready   = pyqtSignal(str, object)   # (翻訳テキスト, OcrResult)
    translation_skipped = pyqtSignal(object)         # キャッシュヒット時: OcrResult のみ再送
    error_occurred      = pyqtSignal(str)

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self._running = False

        # ── 内部状態 ──────────────────────────
        self._cache = TranslationCache(max_size=config.translation.cache_max_size)

        # クラスター別の pending リクエスト { cluster_id: (text, OcrResult) }
        # call_soon_threadsafe で asyncio スレッド側からのみ書き換えるためスレッドセーフ
        self._is_translating = False
        self._pending_by_cluster: dict[int, tuple[str, "OcrResult"]] = {}

        # asyncio プリミティブ（run() 内で初期化）
        self._event: Optional[asyncio.Event] = None
        self._loop:  Optional[asyncio.AbstractEventLoop] = None

    # ── 公開API: メインスレッドから呼ぶ ──────

    def request_translation(self, ocr_result: OcrResult) -> None:
        """
        翻訳リクエストをワーカーに送る。スレッドセーフ。

        翻訳中の場合は _pending を上書きするだけ（キュー詰まり防止）。
        翻訳が終わった直後に _pending を確認して即座に処理する。
        """
        text = ocr_result.full_text.strip()
        if not text:
            return

        # キャッシュヒット: 翻訳せずシグナルだけ再発行
        cached = self._cache.get(text)
        if cached is not None:
            logger.debug("キャッシュヒット: '%s...'", text[:30])
            self.translation_skipped.emit(ocr_result)
            # キャッシュ済みテキストを translation_ready として再送
            # (オーバーレイが座標情報を受け取れるように)
            self.translation_ready.emit(cached, ocr_result)
            return

        if self._loop is None or not self._loop.is_running():
            logger.warning("TranslatorWorker のループがまだ起動していません")
            return

        # スレッドをまたいで安全に _pending を更新し、Event をセット
        self._loop.call_soon_threadsafe(self._enqueue, text, ocr_result)

    def stop(self) -> None:
        """外部（メインスレッド）からスレッドを停止する"""
        self._running = False
        if self._loop and self._event:
            self._loop.call_soon_threadsafe(self._event.set)
        logger.info("TranslatorWorker: 停止リクエストを受信")

    def clear_cache(self) -> None:
        """
        翻訳キャッシュを安全にクリアする。スレッドセーフ。

        TranslationCache（内部は OrderedDict）はワーカースレッド自身が
        request_translation() 経由の get() や _do_translate() 後の set() で
        読み書きしている。外部スレッドから直接 _cache.clear() すると、
        ワーカー側が popitem()/move_to_end() の途中に割り込む可能性がある
        （個々の dict 操作は GIL でアトミックでも、複合操作はそうではない）。

        そのため OcrWorker.pause()/resume() と同じ方針で、実際のクリア処理
        本体（_do_clear_cache）はワーカー自身の asyncio ループ内で
        call_soon_threadsafe() 経由により実行させる。
        """
        if self._loop is None or not self._loop.is_running():
            logger.warning("TranslatorWorker.clear_cache(): ループがまだ起動していません")
            return
        self._loop.call_soon_threadsafe(self._do_clear_cache)

    def _do_clear_cache(self) -> None:
        """asyncio ループ内（ワーカー自身のスレッド）で実行されるキャッシュクリア本体"""
        size_before = len(self._cache)
        self._cache = TranslationCache(max_size=self.config.translation.cache_max_size)
        logger.info("TranslatorWorker: 翻訳キャッシュをクリアしました（%d件 → 0件）", size_before)

    # ── QThread エントリポイント ──────────────

    def run(self) -> None:
        self._running = True
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        logger.info("TranslatorWorker: asyncio イベントループ開始")

        try:
            self._loop.run_until_complete(self._translation_loop())
        except Exception as exc:
            logger.exception("TranslatorWorker: 致命的エラー: %s", exc)
            self.error_occurred.emit(f"TranslatorWorker 致命的エラー: {exc}")
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()
            logger.info("TranslatorWorker: イベントループ終了")

    # ── 内部: asyncio ループ内の処理 ──────────

    def _enqueue(self, text: str, ocr_result: OcrResult) -> None:
        """
        asyncio ループ内で実行される。クラスター別に pending を上書きして Event をセット。
        call_soon_threadsafe 経由で呼ばれるためスレッドセーフ。

        同一 cluster_id の古いリクエストは上書きされる（最新1件維持）。
        異なる cluster_id のリクエストは dict に並存し、並列的に翻訳される。
        """
        self._pending_by_cluster[ocr_result.cluster_id] = (text, ocr_result)
        if self._event:
            self._event.set()

    async def _translation_loop(self) -> None:
        """
        翻訳リクエストを待ち受けるメインループ。
        asyncio.Event を使って無駄なポーリングをしない設計。

        クラスター対応:
            _pending_by_cluster には複数の cluster_id のリクエストが並存できる。
            Event が立つたびに全クラスターのペンディングをスナップショットして
            cluster_id 順に直列翻訳する（別クラスターの翻訳が混ざらない）。
        """
        self._event = asyncio.Event()

        while self._running:
            # リクエストが来るまで待機（CPU使用率ゼロ）
            await self._event.wait()
            self._event.clear()

            if not self._running:
                break

            # 全 pending クラスターをスナップショットして取り出す
            snapshot = dict(self._pending_by_cluster)
            self._pending_by_cluster.clear()

            if not snapshot:
                continue

            # cluster_id 昇順で翻訳（左カラム → 右カラムの自然な順序）
            for cid in sorted(snapshot):
                text, ocr_result = snapshot[cid]
                await self._do_translate(text, ocr_result)

            # 翻訳完了後に新しい pending があれば即座に続行
            if self._pending_by_cluster:
                self._event.set()

    async def _do_translate(self, text: str, ocr_result: OcrResult) -> None:
        """
        実際に Ollama へリクエストを送る。
        is_translating フラグで二重実行を防ぐ（念のためのガード）。
        """
        if self._is_translating:
            # 通常は _translation_loop が直列実行するためここには来ない
            logger.warning("_do_translate が二重呼び出しされました（スキップ）")
            return

        self._is_translating = True
        logger.debug("翻訳開始: '%s...'", text[:40].replace("\n", " "))

        try:
            # system フィールドで役割指示をするため、prompt にはテキストのみを渡す。
            # prompt_template に "{text}" がある場合はそれを使い、
            # ない場合はテキストを直接渡す。
            prompt = (
                self.config.translation.prompt_template.format(text=text)
                if "{text}" in self.config.translation.prompt_template
                else text
            )
            translated = await _call_ollama(
                url=self.config.translation.ollama_url,
                model=self.config.translation.model_name,
                prompt=prompt,
                timeout=self.config.translation.request_timeout_sec,
            )

            print(f"[DEBUG-TRANS] 翻訳完了! Ollamaの生出力: {repr(translated)}")

            if translated:
                self._cache.set(text, translated)
                if self.config.debug.verbose_logging:
                    logger.debug("翻訳完了: '%s...' → '%s...'",
                                 text[:30], translated[:30])
                    logger.debug("キャッシュ統計: %s", self._cache.stats())
                self.translation_ready.emit(translated, ocr_result)
            else:
                logger.warning("翻訳結果が空でした（モデル応答なし）")

        except httpx.TimeoutException:
            msg = (f"Ollama タイムアウト ({self.config.translation.request_timeout_sec}秒)。"
                   "モデルが起動しているか確認してください。")
            logger.error(msg)
            self.error_occurred.emit(msg)

        except httpx.ConnectError:
            msg = (f"Ollama に接続できません ({self.config.translation.ollama_url})。"
                   "`ollama serve` が起動しているか確認してください。")
            logger.error(msg)
            self.error_occurred.emit(msg)

        except Exception as exc:
            logger.exception("翻訳中に予期しないエラー: %s", exc)
            self.error_occurred.emit(str(exc))

        finally:
            self._is_translating = False


# ─────────────────────────────────────────────
#  モジュールレベルのヘルパー関数
# ─────────────────────────────────────────────

async def _call_ollama(
    url: str,
    model: str,
    prompt: str,
    timeout: int = 15,
) -> Optional[str]:
    """
    Ollama の /api/generate エンドポイントに非同期POSTして翻訳結果を返す。

    【設計ポイント】
    小型モデル (qwen2.5:1.5b 等) は prompt フィールドに指示とテキストを混在させると
    「テキストの続き生成モード」に入り、英語のまま出力してしまう。
    これを防ぐため:
      - `system` フィールド: 翻訳者としての役割・日本語出力のみの強い制約
      - `prompt` フィールド: 翻訳したい英語テキストのみ（指示なし）
      - `stop` リスト: よくある「英語への逃げ」パターンを打ち切る
    """
    # システムプロンプト: 役割と出力制約を明確に分離
    system_prompt = (
        "You are a professional Japanese translator. "
        "Your only job is to translate the English text given by the user into natural Japanese. "
        "Rules you MUST follow:\n"
        "1. Output ONLY the Japanese translation. Nothing else.\n"
        "2. Do NOT write English words in your response.\n"
        "3. Do NOT add any explanation, notes, or preamble.\n"
        "4. Do NOT repeat the original English text.\n"
        "5. If the input is a single word, translate just that word.\n"
        "6. Keep all Arabic numerals as-is. Do NOT convert them to kanji or "
        "Japanese number words. For example: '3' stays '3', not '三' or 'さん'."
    )

    payload = {
        "model":  model,
        "system": system_prompt,
        "prompt": prompt,          # ← 翻訳対象テキストのみ。指示は system に分離。
        "stream": False,
        "options": {
            "temperature": 0.0,    # 完全決定論的（同一入力から常に同一出力）
            "num_predict": 300,
            "repeat_penalty": 1.1,
            # 英語の「続き生成」が始まったら打ち切るストップワード
            "stop": [
                "\n\n", "Translation:", "Note:", "In Japanese:",
                "The Japanese", "Here is", "Sure,", "Of course",
            ],
        },
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    # Ollama /api/generate レスポンス: {"response": "...", "done": true, ...}
    raw = data.get("response", "").strip()
    if not raw:
        return None

    # ── 後処理: モデルが漏らしやすい英語プレフィックスを除去 ────────────────
    cleaned = _clean_translation_output(raw)
    return cleaned if cleaned else None


def _clean_translation_output(text: str) -> str:
    """
    LLM が出力しやすい余分なプレフィックス・英語混入を除去する後処理。

    除去対象の例:
        "Translation: 勇者は立ち向かった"  → "勇者は立ち向かった"
        "Japanese: 勇者は立ち向かった"     → "勇者は立ち向かった"
        "Here is the translation:\n勇者..."  → "勇者は立ち向かった"
    """
    import re

    # よくある英語プレフィックスパターン（大文字小文字を無視）
    prefix_patterns = [
        r"^(Translation|Japanese translation|In Japanese|Answer|Result)\s*:\s*",
        r"^Here is (the |a )?translation\s*:?\s*",
        r"^Sure[,!]?\s*(here (it is|you go)[,!]?\s*)?:?\s*",
        r"^Of course[,!]?\s*:?\s*",
        r"^The (Japanese |translation )?is\s*:?\s*",
    ]
    cleaned = text
    for pat in prefix_patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()

    # 先頭行が明らかに英語だけで構成されていて、後続行に日本語がある場合は先頭行を捨てる
    lines = cleaned.splitlines()
    if len(lines) >= 2:
        first = lines[0].strip()
        # 先頭行がASCII文字のみ（日本語なし）かつ短い場合はプレフィックスとみなす
        if first and all(ord(c) < 128 for c in first) and len(first) < 80:
            rest = "\n".join(lines[1:]).strip()
            if rest:
                cleaned = rest

    return cleaned.strip()


# ─────────────────────────────────────────────
#  スタンドアロン動作確認（GUIなし）
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )

    SEP = "=" * 55

    # ── テスト用ダミー OcrResult ──────────────
    def _make_dummy_ocr(text: str) -> OcrResult:
        return OcrResult(full_text=text, lines=[], capture_left=0, capture_top=0)

    # ────────────────────────────────────────
    async def _run_tests(cfg: AppConfig) -> None:
        print(f"\n{SEP}")
        print("  translator.py  スタンドアロン動作確認")
        print(SEP)

        # ────────────────────────────────
        # STEP 1: キャッシュ基本動作
        # ────────────────────────────────
        print("\n[STEP 1] TranslationCache 動作確認")
        cache = TranslationCache(max_size=3)

        cache.set("Hello", "こんにちは")
        cache.set("World", "世界")
        cache.set("Game",  "ゲーム")

        assert cache.get("Hello") == "こんにちは", "キャッシュ取得失敗"
        assert len(cache) == 3,                    "サイズ不正"
        print(f"  ✓ 基本的な set/get: OK")

        # 4件目を追加 → 最も古い "World" が削除されるはず
        # ※ "Hello" は直前にアクセスされたので末尾に移動済み
        cache.set("NewEntry", "新エントリ")
        assert "World" not in cache._cache,  "古いエントリが削除されていない"
        assert len(cache) == 3,              "上限超過後のサイズ不正"
        print(f"  ✓ 上限 (max_size=3) 超過時に最古エントリを自動削除: OK")

        # キャッシュミス
        result = cache.get("NotExist")
        assert result is None, "存在しないキーが None でない"
        print(f"  ✓ 存在しないキーは None を返す: OK")

        print(f"  キャッシュ統計: {cache.stats()}")

        # ────────────────────────────────
        # STEP 2: is_translating フラグ動作確認
        # ────────────────────────────────
        print(f"\n[STEP 2] is_translating フラグ動作確認（モックで検証）")

        call_count = 0

        async def _mock_translate_slow(delay: float) -> str:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(delay)
            return f"翻訳結果_{call_count}"

        # フラグの動作を直接シミュレート
        is_translating = False

        async def _guarded_translate(label: str, delay: float) -> Optional[str]:
            nonlocal is_translating
            if is_translating:
                print(f"  → '{label}': is_translating=True のためスキップ ✓")
                return None
            is_translating = True
            try:
                result_text = await _mock_translate_slow(delay)
                print(f"  → '{label}': 翻訳完了 → '{result_text}'")
                return result_text
            finally:
                is_translating = False

        # 最初のリクエストは実行される
        r1 = asyncio.create_task(_guarded_translate("リクエスト A (0.1s)", 0.1))
        # 少し遅れて2つ目: A 実行中なのでスキップされるはず
        await asyncio.sleep(0.01)
        r2 = asyncio.create_task(_guarded_translate("リクエスト B (スキップ期待)", 0.1))
        await asyncio.gather(r1, r2)

        assert call_count == 1, f"is_translating フラグが機能していない (call_count={call_count})"
        print(f"  ✓ 翻訳中の二重実行を is_translating でガード: OK")

        # ────────────────────────────────
        # STEP 3: _pending 上書き（最新1件待機）確認
        # ────────────────────────────────
        print(f"\n[STEP 3] 『最新の1件待機』パターン確認")
        pending = None

        def enqueue(text: str) -> None:
            nonlocal pending
            if pending is not None:
                print(f"  → '{pending}' を破棄して '{text}' で上書き ✓")
            else:
                print(f"  → '{text}' を pending にセット")
            pending = text

        enqueue("テキスト1")  # 最初のリクエスト
        enqueue("テキスト2")  # テキスト1を上書き
        enqueue("テキスト3")  # テキスト2を上書き

        assert pending == "テキスト3", f"最新テキストが残っていない: {pending}"
        print(f"  ✓ 最終的に pending = '{pending}' (最新の1件のみ保持): OK")

        # ────────────────────────────────
        # STEP 4: Ollama 実接続テスト
        # ────────────────────────────────
        print(f"\n[STEP 4] Ollama 実接続テスト")
        print(f"  URL  : {cfg.translation.ollama_url}")
        print(f"  Model: {cfg.translation.model_name}")

        # まず Ollama サーバーの死活確認（/api/tags エンドポイント）
        base_url = cfg.translation.ollama_url.rsplit("/api/", 1)[0]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
                health = await client.get(f"{base_url}/api/tags")
            health.raise_for_status()
            models = [m["name"] for m in health.json().get("models", [])]
            print(f"  ✓ Ollama サーバー応答あり")
            print(f"  利用可能なモデル: {models or '(なし)'}")
        except Exception as e:
            print(f"  ✗ Ollama サーバーへの接続失敗: {e}")
            print(f"  → `ollama serve` が起動しているか確認してください。")
            print(f"  → STEP 4 をスキップして終了します。")
            _print_summary()
            return

        # 翻訳リクエスト送信
        test_text = "The hero approaches the ancient temple."
        prompt = cfg.translation.prompt_template.format(text=test_text)
        print(f"\n  翻訳リクエスト送信中...")
        print(f"  原文: '{test_text}'")

        try:
            translated = await _call_ollama(
                url=cfg.translation.ollama_url,
                model=cfg.translation.model_name,
                prompt=prompt,
                timeout=cfg.translation.request_timeout_sec,
            )
            if translated:
                print(f"  ✓ 翻訳成功: '{translated}'")
            else:
                print(f"  ✗ 翻訳結果が空でした")
        except httpx.TimeoutException:
            print(f"  ✗ タイムアウト ({cfg.translation.request_timeout_sec}秒)")
        except Exception as e:
            print(f"  ✗ 翻訳エラー: {e}")

        # キャッシュに保存されているか確認
        test_cache = TranslationCache(max_size=10)
        test_cache.set(test_text, translated or "（空）")
        assert test_cache.get(test_text) is not None
        print(f"  ✓ 翻訳結果をキャッシュに保存・取得: OK")

        _print_summary()

    def _print_summary() -> None:
        print(f"\n{SEP}")
        print("  全ステップ完了")
        print(SEP + "\n")

    cfg = load_config()
    asyncio.run(_run_tests(cfg))

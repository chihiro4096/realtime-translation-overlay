"""
ocr_engine.py - スクリーンキャプチャ & Windows Media OCR ワーカー

アーキテクチャ（スレッド構造）:
    メインスレッド (PyQt6 UI)
        └─ OcrWorker(QThread).start()
                └─ run() → asyncio.new_event_loop()
                        └─ _main_loop()
                                ├─ _maybe_refresh_window_rect()  ← pygetwindow
                                ├─ _capture_screen()             ← mss
                                └─ _do_ocr()                     ← Windows Media OCR

シグナル（QThread → メインスレッド）:
    text_detected(OcrResult)   : 新しいテキストを検知したとき
    window_not_found()         : ターゲットウィンドウが見つからないとき
    status_changed(str)        : ステータス表示用メッセージ
    error_occurred(str)        : エラーが発生したとき

公開制御API（メインスレッド → QThread、すべてスレッドセーフ）:
    pause()  : asyncio.Event を clear() し、次サイクル開始前で待機させる
    resume() : asyncio.Event を set() し、待機を解除する
    どちらも call_soon_threadsafe() 経由でイベントループに委譲される。
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace as dataclass_replace
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from typing import Optional

import re
import threading
import mss
from PIL import Image, ImageDraw
from PyQt6.QtCore import QThread, pyqtSignal

# Windows Media OCR (winsdk)
from winsdk.windows.globalization import Language
from winsdk.windows.graphics.imaging import (
    BitmapAlphaMode,
    BitmapDecoder,
    BitmapPixelFormat,
    SoftwareBitmap,
)
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

from config import AppConfig, load_config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  データクラス: OCR結果の受け渡し型
# ─────────────────────────────────────────────

class MaskRegionStore:
    """
    SubtitleManager（GUIスレッド）と OcrWorker（OCRスレッド）間で
    「現在画面に表示中の字幕領域」を受け渡すスレッドセーフな共有コンテナ。

    【自己マスキング (Self-Masking) の中核】
    OBS 等の配信には字幕を映したまま、OCR には自分の字幕を読ませない
    ようにするため、以下のフローで動作する:

        SubtitleManager (GUIスレッド)
            字幕ラベルを配置するたびに、各ラベルの画面絶対座標
            （物理ピクセル）を update() で書き込む
                ↓
        OcrWorker (OCRスレッド)
            _capture_screen() でキャプチャした直後、get() で取得した
            矩形領域を黒で塗りつぶしてから OCR エンジンに渡す
                ↓
        結果: OCR は自分自身が描いた字幕を認識しない
              （ハウリング防止）。一方 mss で塗りつぶすのは
              OcrWorker 内のコピー画像のみなので、画面上の
              OverlayWindow 自体は何も変更されず、OBS には
              通常通り字幕が映る。

    座標系: 全て Win32 物理ピクセルの画面絶対座標 (x, y, w, h)。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._regions: list[tuple[int, int, int, int]] = []

    def update(self, regions: list[tuple[int, int, int, int]]) -> None:
        """字幕領域リストを書き込む（GUIスレッドから呼ばれる）"""
        with self._lock:
            self._regions = list(regions)

    def get(self) -> list[tuple[int, int, int, int]]:
        """字幕領域リストのコピーを取得する（OCRスレッドから呼ばれる）"""
        with self._lock:
            return list(self._regions)


class _ClusterTracker:
    """
    フレーム間でクラスターの空間的な同一性を追跡し、永続的なIDを割り当てる
    軽量トラッカー（IoUベース・貪欲マッチング）。

    ── 解決する問題 ─────────────────────────────────────────────────────────────
    _cluster_lines() は毎フレーム独立に再計算される純粋関数であり、記憶を持たない。
    返されるクラスターのリスト順（= enumerate による配列インデックス）は、
    検出数や検出順序のわずかな変化（OCRのブレで1行検出されたりされなかったり）
    だけで簡単に入れ替わる。

    これを cluster_id としてそのまま使うと:
      - 同じ空間にある同じパネルが、フレームごとに違う cluster_id になる
      - _missing_count による幽霊字幕デバウンスが正しく機能しない
        （IDが変わるたびにカウントが0からリセットされてしまう）
      - 将来のフレーム間多数決機構（過去Nフレームの集計）も同様に破綻する

    ── 解決方法 ─────────────────────────────────────────────────────────────────
    各フレームで検出された raw cluster の BoundingBox（キャプチャ内ローカル座標）
    を計算し、前フレームまでに記憶している persistent ID の BoundingBox と
    IoU (Intersection over Union) を比較する。
    最もIoUが高く、かつ閾値を超える組み合わせを貪欲法でマッチングし、
    マッチした場合は既存の persistent ID を引き継ぐ。マッチしなければ
    新規IDを発行する（ID は単調増加、一度発行したら使い回さない）。

    ── 記憶の保持と解放 ─────────────────────────────────────────────────────────
    あるフレームで検出されなかった persistent ID も、即座には記憶から
    削除しない（OcrWorker側の _missing_count デバウンスが「猶予期間」を
    判断するため、その間は古い BoundingBox を保持して再出現時の
    マッチ候補にする必要がある）。
    OcrWorker._tick() がデバウンス猶予を超えて「完全消滅」と判定した時点で
    forget() を呼び出し、トラッカーの記憶から明示的に削除する。

    座標系: capture画像内のローカル座標 (local_x/y) を使う。
    画面上の絶対座標 (screen_x/y) はウィンドウが移動すると変化するため、
    ウィンドウ内コンテンツの相対位置が安定しているローカル座標系が適切。
    """

    def __init__(self, iou_threshold: float = 0.15) -> None:
        self._iou_threshold = iou_threshold
        self._next_id: int = 0
        # persistent_id → 直近の BoundingBox (min_x, min_y, max_x, max_y)
        self._tracked_boxes: dict[int, tuple[int, int, int, int]] = {}

    @staticmethod
    def _bbox_of(cluster: list["LineRect"]) -> tuple[int, int, int, int]:
        """クラスター内の全 LineRect を包含する BoundingBox を計算する"""
        min_x = min(lr.local_x for lr in cluster)
        min_y = min(lr.local_y for lr in cluster)
        max_x = max(lr.local_x + lr.local_w for lr in cluster)
        max_y = max(lr.local_y + lr.local_h for lr in cluster)
        return (min_x, min_y, max_x, max_y)

    @staticmethod
    def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
        """2つの BoundingBox 間の IoU (Intersection over Union) を計算する"""
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        ix0, iy0 = max(ax0, bx0), max(ay0, by0)
        ix1, iy1 = min(ax1, bx1), min(ay1, by1)
        iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = max(0, ax1 - ax0) * max(0, ay1 - ay0)
        area_b = max(0, bx1 - bx0) * max(0, by1 - by0)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    def assign(self, raw_clusters: list[list["LineRect"]]) -> list[int]:
        """
        今フレームの raw_clusters に対して永続的な cluster_id を割り当てる。

        Args:
            raw_clusters: _cluster_lines() が返すクラスターのリスト

        Returns:
            raw_clusters と同じ順序・同じ長さの persistent_id のリスト
        """
        if not raw_clusters:
            return []

        new_boxes = [self._bbox_of(c) for c in raw_clusters]

        # ── 貪欲マッチング: 全(新,旧)ペアのIoUを計算し、高い順に確定させる ──
        candidates: list[tuple[float, int, int]] = []  # (iou, new_idx, persistent_id)
        for new_idx, nb in enumerate(new_boxes):
            for pid, ob in self._tracked_boxes.items():
                iou = self._iou(nb, ob)
                if iou >= self._iou_threshold:
                    candidates.append((iou, new_idx, pid))
        candidates.sort(key=lambda t: t[0], reverse=True)

        assigned: list[Optional[int]] = [None] * len(raw_clusters)
        used_pids: set[int] = set()
        for iou, new_idx, pid in candidates:
            if assigned[new_idx] is not None or pid in used_pids:
                continue  # どちらかが既に確定済み → このペアはスキップ
            assigned[new_idx] = pid
            used_pids.add(pid)

        # マッチしなかった新クラスターには新規IDを発行
        for i, pid in enumerate(assigned):
            if pid is None:
                assigned[i] = self._next_id
                self._next_id += 1

        final_ids: list[int] = assigned  # type: ignore[assignment]

        # 今フレームに出現した全クラスターの BoundingBox で記憶を更新
        # （forget() されない限り、出現しなかった旧IDの記憶はそのまま残る）
        for i, pid in enumerate(final_ids):
            self._tracked_boxes[pid] = new_boxes[i]

        return final_ids

    def forget(self, persistent_id: int) -> None:
        """
        完全消滅が確定した persistent_id をトラッカーの記憶から削除する。
        OcrWorker._tick() のデバウンス処理から呼ばれる。
        """
        self._tracked_boxes.pop(persistent_id, None)


@dataclass
class LineRect:
    """1行分のテキストと画面上の絶対座標"""
    text: str
    # キャプチャ領域内でのローカル座標 (OCRが返す値)
    local_x: int
    local_y: int
    local_w: int
    local_h: int
    # 画面上での絶対座標（オーバーレイ配置に使用）
    screen_x: int
    screen_y: int


@dataclass
class OcrResult:
    """
    OcrWorker が text_detected シグナルで送出するデータ。
    翻訳テキストのオーバーレイ位置計算に必要な情報をすべて含む。
    """
    full_text: str                      # 全行連結のフルテキスト
    lines: list[LineRect] = field(default_factory=list)  # ライン別の座標付き結果
    capture_left: int = 0               # キャプチャ領域の画面上での左端X
    capture_top: int = 0                # キャプチャ領域の画面上での上端Y
    cluster_id: int = 0                 # 空間クラスター識別子（0始まり）


# ─────────────────────────────────────────────
#  OcrWorker: QThread + asyncio のカプセル化
# ─────────────────────────────────────────────

class OcrWorker(QThread):
    """
    OCR処理を独立したスレッドで実行するワーカー。

    PyQt6のメインスレッドを一切ブロックしない設計になっている。
    asyncio の WinRT 非同期呼び出しは run() 内の専用イベントループに閉じ込める。
    """

    # ── シグナル定義 ──────────────────────────
    text_detected = pyqtSignal(object)   # OcrResult インスタンスを渡す
    window_not_found = pyqtSignal()
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        config: AppConfig,
        parent=None,
        mask_store: Optional["MaskRegionStore"] = None,
    ):
        super().__init__(parent)
        self.config = config
        self._running = False

        # ウィンドウ矩形キャッシュ: (x, y, width, height) or None
        self._window_rect: Optional[tuple[int, int, int, int]] = None
        self._last_window_search: float = 0.0

        # 差分検知用: クラスター別の前回テキスト { cluster_id: text }
        self._last_text_by_cluster: dict[int, str] = {}

        # 幽霊字幕デバウンス用: クラスター別の連続欠落フレーム数 (提案1)
        # { cluster_id: 連続欠落フレーム数 }
        # cluster_disappear_frames に達したら字幕クリアを emit する
        self._missing_count: dict[int, int] = {}

        # 誕生時デバウンス: PENDING（仮）→ CONFIRMED（確定）の状態管理
        # _confirmed_ids: 本物と確定したクラスターID
        # _pending_count:  未確定IDの連続検出フレーム数 { cluster_id: count }
        self._confirmed_ids: set[int] = set()
        self._pending_count: dict[int, int] = {}

        # 空間トラッキング: フレーム間でクラスターの同一性を追跡し、
        # 永続的な cluster_id を割り当てる（配列インデックスの不安定さを解消）
        self._cluster_tracker = _ClusterTracker(
            iou_threshold=config.ocr.cluster_tracking_iou_threshold
        )

        # フレーム間多数決用投票バッファ
        # { persistent_cluster_id: deque[str, maxlen=majority_vote_frames] }
        # CONFIRMED クラスターの直近 N フレーム分の OCR テキストを保持し、
        # _plurality_vote() で代表テキストを決定するために使う。
        # クラスターが消滅確定（forget）されるタイミングで対応エントリを削除する。
        self._text_votes: dict[int, deque] = {}

        # ── ヒステリシス（グレーゾーン確定待ち） ────────────────────────────────
        # グレーゾーン（similarity_lower <= sim < similarity_upper）に入った
        # テキストについて、text_confirm_frames フレーム連続で同一テキストが
        # 来たときだけ emit する「確定待ち」バッファ。
        #
        # { cluster_id: (candidate_text, consecutive_count) }
        #   candidate_text   : 現在確定待ち中のテキスト
        #   consecutive_count: 同一テキストの連続フレーム数
        self._grey_pending: dict[int, tuple[str, int]] = {}

        # 自己マスキング用の共有ストア（None ならマスク処理は無効）
        self._mask_store = mask_store

        # ── 一時停止制御（タスクトレイ「一時停止/再開」用） ────────────────────
        # _pause_event は run() 内で asyncio.Event() として生成する。
        # set() = 稼働中／clear() = 一時停止中、という向きにしておくことで
        # 「待つ側（_tick の手前）」が await wait() するだけで自然に止まる。
        # メインスレッドから操作する場合は必ず _loop.call_soon_threadsafe() を
        # 経由すること（asyncio.Event はスレッドセーフではないため）。
        self._pause_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._paused: bool = False  # メインスレッドからも参照する状態フラグ（読み取り専用用途）

    # ── QThread エントリポイント ──────────────

    def run(self) -> None:
        """
        QThread.start() で呼ばれる。
        このスレッド専用の asyncio イベントループを作成して実行する。
        """
        self._running = True
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # 初期状態は「稼働中」
        logger.info("OcrWorker: asyncio イベントループ開始")

        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as exc:
            logger.exception("OcrWorker: 予期しない致命的エラー: %s", exc)
            self.error_occurred.emit(f"OCRワーカー致命的エラー: {exc}")
        finally:
            try:
                # 残留タスクをキャンセル
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            finally:
                self._loop.close()
                logger.info("OcrWorker: イベントループ終了")

    def stop(self) -> None:
        """外部（メインスレッド）からスレッドを停止する"""
        self._running = False
        # 一時停止中に停止指示が来た場合、wait() でブロックしたままだと
        # ループが _running を再チェックできず終了しない。必ず起こしてやる。
        if self._loop is not None and self._pause_event is not None:
            self._loop.call_soon_threadsafe(self._pause_event.set)
        logger.info("OcrWorker: 停止リクエストを受信")

    # ── 公開API: メインスレッドから呼ぶ（タスクトレイ用） ──────────────────

    def pause(self) -> None:
        """
        OCR ループを安全にサスペンドする。スレッドセーフ。

        実行中の _tick() を中断するのではなく、次回サイクルの開始前
        （await の地点）で待機させる「協調的な一時停止」。
        キャプチャ・OCR・Win32 API 呼び出しの途中で停止することはない。
        """
        if self._loop is None or self._pause_event is None:
            logger.warning("OcrWorker.pause(): イベントループがまだ起動していません")
            return
        self._paused = True
        self._loop.call_soon_threadsafe(self._pause_event.clear)
        logger.info("OcrWorker: 一時停止しました")

    def resume(self) -> None:
        """一時停止を解除する。スレッドセーフ。"""
        if self._loop is None or self._pause_event is None:
            logger.warning("OcrWorker.resume(): イベントループがまだ起動していません")
            return
        self._paused = False
        self._loop.call_soon_threadsafe(self._pause_event.set)
        logger.info("OcrWorker: 再開しました")

    @property
    def is_paused(self) -> bool:
        return self._paused

    # ── メインループ ──────────────────────────

    async def _main_loop(self) -> None:
        """キャプチャ → OCR を capture_interval_ms ごとに繰り返す"""
        interval = self.config.capture.capture_interval_ms / 1000.0

        while self._running:
            # 一時停止中はここでブロックする（CPU/GPU 負荷ゼロ）。
            # resume() が呼ばれて Event が set されるまで先に進まない。
            await self._pause_event.wait()
            if not self._running:
                break

            t_start = asyncio.get_event_loop().time()

            try:
                await self._tick()
            except Exception as exc:
                logger.error("_tick() エラー: %s", exc)
                self.error_occurred.emit(str(exc))

            # 処理時間を差し引いて正確なインターバルを保つ
            elapsed = asyncio.get_event_loop().time() - t_start
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _tick(self) -> None:
        """1サイクル: ウィンドウ確認 → キャプチャ → OCR → 差分判定 → シグナル"""

        await self._maybe_refresh_window_rect()

        if self._window_rect is None:
            self.window_not_found.emit()
            return

        # ── キャプチャ ──
        img, cap_left, cap_top = self._capture_screen()
        if img is None:
            return

        # ── OCR → クラスターリスト ──
        results: list[OcrResult] = await self._do_ocr(img, cap_left, cap_top)

        # 今回フレームで検出されたクラスター ID の集合
        current_ids = {r.cluster_id for r in results}

        disappear_thresh = self.config.ocr.cluster_disappear_frames
        confirm_thresh = self.config.ocr.cluster_confirm_frames

        # ── 欠落クラスターのデバウンス判定 (提案1) ──────────────────────────
        # CONFIRMED 状態のクラスターのみ対象。
        # 「今フレームで検出されなかった確定クラスター」をただちに消去するのではなく、
        # cluster_disappear_frames フレーム連続で欠落した時点で初めて消滅確定とする。
        for cid in list(self._confirmed_ids):
            if cid in current_ids:
                self._missing_count.pop(cid, None)
            else:
                count = self._missing_count.get(cid, 0) + 1
                if count >= disappear_thresh:
                    self._confirmed_ids.discard(cid)
                    self._last_text_by_cluster.pop(cid, None)
                    self._missing_count.pop(cid, None)
                    self._text_votes.pop(cid, None)   # 投票バッファも一緒に解放
                    self._grey_pending.pop(cid, None)  # ヒステリシスバッファも解放
                    self._cluster_tracker.forget(cid)
                    logger.debug(
                        "cluster=%d が %d フレーム連続欠落。字幕クリアを通知します。",
                        cid, disappear_thresh,
                    )
                    self.text_detected.emit(OcrResult(
                        full_text="",
                        lines=[],
                        capture_left=cap_left,
                        capture_top=cap_top,
                        cluster_id=cid,
                    ))
                else:
                    self._missing_count[cid] = count

        # ── 誕生時デバウンス: PENDING → CONFIRMED ───────────────────────────
        # 背景ノイズ（YouTube UI 等）の一瞬検出で新規IDが発行されても、
        # 連続検出が confirm_thresh に達するまで翻訳・表示しない。
        # PENDING 中に1フレームでも見失ったIDは猶予なしで即破棄する。
        for cid in list(self._pending_count):
            if cid not in current_ids:
                self._pending_count.pop(cid, None)
                self._cluster_tracker.forget(cid)
                logger.debug(
                    "cluster=%d PENDING欠落。ノイズとして即破棄します。", cid,
                )

        for cid in current_ids:
            if cid in self._confirmed_ids:
                continue
            count = self._pending_count.get(cid, 0) + 1
            self._pending_count[cid] = count
            if count >= confirm_thresh:
                self._confirmed_ids.add(cid)
                self._pending_count.pop(cid, None)
                logger.debug(
                    "cluster=%d CONFIRMEDに昇格（%dフレーム連続検出）", cid, count,
                )

        # ── クラスター別差分検知（CONFIRMED のみ）────────────────────────────
        # 多数決ロジックの挿入点:
        #   raw OCR テキスト → 投票バッファに追加 → 多数決で代表テキストを決定
        #   → 代表テキストをヒステリシス3段階判定の対象にする
        #
        # _last_text_by_cluster には「最後に emit した代表テキスト」を保持する
        #（raw OCR テキストではなく、投票済みテキスト）。
        # こうすることで類似度チェックが
        # 「ユーザーが今画面で見ているテキスト vs 最新の多数決結果」
        # という正しい比較になる。
        vote_window  = self.config.ocr.majority_vote_frames
        upper_thresh = self.config.ocr.similarity_threshold        # ≥ この値 → 変化なし
        lower_thresh = self.config.ocr.similarity_lower_threshold  # < この値 → 即 emit
        confirm_frames = self.config.ocr.text_confirm_frames       # グレーゾーン確定フレーム数

        for result in results:
            cid = result.cluster_id
            if cid not in self._confirmed_ids:
                continue

            # ── 投票バッファへ追加 ─────────────────────────────────────────
            if cid not in self._text_votes:
                self._text_votes[cid] = deque(maxlen=vote_window)
            elif self._text_votes[cid].maxlen != vote_window:
                old_votes = list(self._text_votes[cid])
                self._text_votes[cid] = deque(old_votes, maxlen=vote_window)
            self._text_votes[cid].append(result.full_text)

            # ── 多数決で代表テキストを決定 ─────────────────────────────────
            voted_text = _plurality_vote(self._text_votes[cid])

            # ── 類似度チェック（voted_text vs 前回 emit 済みテキスト）────────
            prev_text  = self._last_text_by_cluster.get(cid, "")
            similarity = _text_similarity(voted_text, prev_text)

            # ════════════════════════════════════════════════════════════════
            # ヒステリシス 3 段階判定
            # ════════════════════════════════════════════════════════════════

            # ── Zone 1: 変化なし ─────────────────────────────────────────
            # 類似度が上限閾値以上 → 何もしない
            if similarity >= upper_thresh:
                # グレーゾーンの確定待ちは無効化する
                # （前回 emit と実質同じテキストに戻ったので候補をリセット）
                self._grey_pending.pop(cid, None)
                if self.config.debug.verbose_logging:
                    logger.debug(
                        "cluster=%d 変化なし（類似度=%.3f）、スキップ"
                        "  [raw=%r  voted=%r]",
                        cid, similarity,
                        result.full_text[:30], voted_text[:30],
                    )
                continue

            # ── Zone 2: 大幅な変化 ──────────────────────────────────────
            # 類似度が下限閾値未満 → グレーゾーン待ちをリセットして即 emit
            if similarity < lower_thresh:
                self._grey_pending.pop(cid, None)
                self._last_text_by_cluster[cid] = voted_text
                logger.debug(
                    "新規検知（大幅変化） cluster=%d（類似度=%.3f）: %s",
                    cid, similarity, voted_text[:60].replace("\n", " "),
                )
                self.text_detected.emit(dataclass_replace(result, full_text=voted_text))
                continue

            # ── Zone 3: グレーゾーン（読みブレ疑いゾーン）───────────────
            # lower_thresh <= similarity < upper_thresh
            # 同一テキストが confirm_frames フレーム連続したときだけ emit する。
            #
            # 「A→B→A→B」の振動:
            #   フレームN  : candidate=B, count=1  （B は A と 0.857 の類似度）
            #   フレームN+1: candidate=A が来る → B と異なる → count リセット
            #   フレームN+2: candidate=B が来る → count=1 から再カウント
            #   → count が confirm_frames に到達しないため emit されない ✓
            #
            # 「Red key → Blue key」の実質的1単語変化:
            #   フレームN  : candidate="...Blue key...", count=1
            #   フレームN+1: candidate="...Blue key...", count=2 → 確定 → emit ✓
            #   （confirm_frames=2 の場合、最大 1 フレーム = 500ms の遅延のみ）
            prev_candidate, prev_count = self._grey_pending.get(cid, ("", 0))

            if voted_text == prev_candidate:
                new_count = prev_count + 1
            else:
                # 候補テキストが変わった → カウントを1からリセット
                new_count = 1

            if new_count >= confirm_frames:
                # 確定: emit してバッファをクリア
                self._grey_pending.pop(cid, None)
                self._last_text_by_cluster[cid] = voted_text
                logger.debug(
                    "新規検知（グレーゾーン確定 %dフレーム） cluster=%d（類似度=%.3f）: %s",
                    new_count, cid, similarity, voted_text[:60].replace("\n", " "),
                )
                self.text_detected.emit(dataclass_replace(result, full_text=voted_text))
            else:
                # 未確定: カウントを更新して次フレームへ持ち越す
                self._grey_pending[cid] = (voted_text, new_count)
                logger.debug(
                    "グレーゾーン確定待ち cluster=%d（類似度=%.3f  %d/%dフレーム）: %s",
                    cid, similarity, new_count, confirm_frames,
                    voted_text[:40].replace("\n", " "),
                )

    # ── ウィンドウ追従 ────────────────────────

    async def _maybe_refresh_window_rect(self) -> None:
        """
        window_search_interval_ms ごとにウィンドウ矩形を再取得する。
        フルスクリーン切り替えやウィンドウ移動・リサイズに追従するための仕組み。
        """
        now = time.monotonic()
        refresh_interval = self.config.target_window.window_search_interval_ms / 1000.0

        # インターバル未到達かつウィンドウ情報が既にある場合はスキップ
        if self._window_rect is not None and \
                (now - self._last_window_search) < refresh_interval:
            return

        self._last_window_search = now
        keyword = self.config.target_window.window_title_keyword
        new_rect = _find_window_rect(keyword)

        if new_rect is None:
            if self._window_rect is not None:
                # 直前まで見えていたのに消えた = ウィンドウが閉じられた可能性
                logger.warning("ウィンドウ '%s' を見失いました", keyword)
                self.status_changed.emit(f"ウィンドウ '{keyword}' を探しています...")
            self._window_rect = None
        else:
            if new_rect != self._window_rect:
                logger.info("ウィンドウ矩形を更新: %s → %s", self._window_rect, new_rect)
                self.status_changed.emit(
                    f"'{keyword}' 検出: ({new_rect[0]}, {new_rect[1]}) "
                    f"{new_rect[2]}x{new_rect[3]}"
                )
            self._window_rect = new_rect

    # ── スクリーンキャプチャ ──────────────────

    def _capture_screen(self) -> tuple[Optional[Image.Image], int, int]:
        """
        ウィンドウ矩形に crop_region 設定を適用して画面をキャプチャする。

        Returns:
            (PIL.Image, capture_left, capture_top)
            capture_left/top は画面上の絶対座標（オーバーレイ位置計算に使用）。
            失敗時は (None, 0, 0)。
        """
        wx, wy, ww, wh = self._window_rect

        crop = self.config.capture.crop_region
        if crop is not None:
            # ウィンドウ相対の比率 → 画面絶対座標に変換
            left   = wx + int(ww * crop.left_ratio)
            top    = wy + int(wh * crop.top_ratio)
            right  = wx + int(ww * crop.right_ratio)
            bottom = wy + int(wh * crop.bottom_ratio)
        else:
            left, top, right, bottom = wx, wy, wx + ww, wy + wh

        region_w = right - left
        region_h = bottom - top

        if region_w <= 0 or region_h <= 0:
            logger.warning("キャプチャ領域が無効: left=%d top=%d w=%d h=%d",
                           left, top, region_w, region_h)
            return None, 0, 0

        monitor = {"left": left, "top": top, "width": region_w, "height": region_h}

        try:
            with mss.mss() as sct:
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)

            # ── 自己マスキング: 自身が描いた字幕領域を塗りつぶす ──────────────
            # mask_store には SubtitleManager が書き込んだ「現在表示中の字幕の
            # 画面絶対座標（物理ピクセル）」が入っている。
            # ここで塗りつぶすのは OCR に渡す画像のコピーのみで、
            # 画面上の OverlayWindow 自体やOBSの映像には一切影響しない。
            if self._mask_store is not None:
                mask_regions = self._mask_store.get()
                if mask_regions:
                    img = _apply_self_mask(img, mask_regions, left, top)

            if self.config.debug.save_captures:
                _save_capture_debug(img)

            return img, left, top

        except Exception as exc:
            logger.error("キャプチャ失敗: %s", exc)
            return None, 0, 0

    # ── Windows Media OCR ─────────────────────

    async def _do_ocr(
        self,
        img: Image.Image,
        capture_left: int,
        capture_top: int,
    ) -> list[OcrResult]:
        """
        Windows Media OCR で画像からテキストを認識し、空間クラスターに分割して返す。

        WinRT の非同期 API は全て await で呼び出す。
        このメソッドは OcrWorker の asyncio ループ内でのみ呼ばれるため安全。

        戻り値: クラスター数分の OcrResult のリスト（空テキスト時は空リスト）
        """
        language = Language(self.config.ocr.language)
        engine = OcrEngine.try_create_from_language(language)

        if engine is None:
            msg = (
                f"OCRエンジン作成失敗。言語パック '{self.config.ocr.language}' が"
                "インストールされているか確認してください。"
            )
            logger.error(msg)
            self.error_occurred.emit(msg)
            return []

        try:
            bitmap = await _pil_to_software_bitmap(img)
            ocr_result = await engine.recognize_async(bitmap)
        except Exception as exc:
            logger.error("OCR認識エラー: %s", exc)
            return []

        # ── 結果の整形: ライン単位で座標を収集（単語レベルの強制X分割込み） ──
        lines: list[LineRect] = []
        for line in ocr_result.lines:
            all_words_text = " ".join(w.text for w in line.words)
            if not all_words_text.strip():
                continue

            # 行の BoundingRect は単語(OcrWord)の bounding_rect からしか
            # 求められない（WinRT の OcrLine 自体には座標プロパティが無い）
            words_with_rect = [
                w for w in line.words
                if hasattr(w, "bounding_rect") and w.bounding_rect is not None
                and w.text.strip()
            ]

            if not words_with_rect:
                # 個別の bounding_rect が一切取得できない場合のフォールバック。
                # 強制分割の判定材料がないため、行全体を1つの LineRect として扱う。
                if not _is_meaningful_line(all_words_text):
                    logger.debug("ノイズ行を除外: %r", all_words_text)
                    continue
                logger.warning(
                    "_do_ocr: 行 %r の単語から bounding_rect を取得できませんでした。"
                    "フォールバック座標を使用します（強制分割は適用されません）。",
                    all_words_text[:30],
                )
                lines.append(LineRect(
                    text=all_words_text,
                    local_x=0, local_y=0, local_w=img.width, local_h=img.height,
                    screen_x=capture_left, screen_y=capture_top,
                ))
                continue

            # ── 単語レベルの強制X分割（Stage1 Step1） ───────────────────────────
            # WinRT は同じベースラインY座標にある単語を、X方向にどれだけ
            # 離れていても1つの OcrLine に結合してしまう仕様があるため、
            # 単語間のXギャップを統計的ギャップ検出で測り、異常に離れた
            # 箇所を境界として強制的に複数の LineRect に分割する。
            word_spans = [
                (w.bounding_rect.x, w.bounding_rect.x + w.bounding_rect.width)
                for w in words_with_rect
            ]
            word_groups_idx = _natural_break_groups(
                word_spans,
                k=self.config.ocr.word_split_gap_k,
                min_gap_px=self.config.ocr.word_split_min_gap_px,
            )

            if len(word_groups_idx) > 1 and self.config.debug.verbose_logging:
                logger.debug(
                    "_do_ocr: WinRTのOcrLineを%d断片に強制分割: %r",
                    len(word_groups_idx), all_words_text[:60],
                )

            for idx_list in word_groups_idx:
                # idx_list は start 順なので、そのまま読み順で連結できる
                group_words = [words_with_rect[i] for i in idx_list]
                sub_text = " ".join(w.text for w in group_words)

                # ── ノイズフィルタ（断片ごとに適用）────────────────────────────
                if not _is_meaningful_line(sub_text):
                    logger.debug("ノイズ行を除外: %r", sub_text)
                    continue

                lx = int(min(w.bounding_rect.x                          for w in group_words))
                ly = int(min(w.bounding_rect.y                          for w in group_words))
                rx = int(max(w.bounding_rect.x + w.bounding_rect.width  for w in group_words))
                by = int(max(w.bounding_rect.y + w.bounding_rect.height for w in group_words))

                lines.append(LineRect(
                    text=sub_text,
                    local_x=lx, local_y=ly, local_w=rx - lx, local_h=by - ly,
                    screen_x=capture_left + lx,
                    screen_y=capture_top  + ly,
                ))

        if not lines:
            return []

        # ── 空間クラスタリング: 統計的ギャップ検出で段落・カラムを分離 ──────────
        raw_clusters = _cluster_lines(
            lines,
            y_scale=self.config.ocr.cluster_y_scale,
            x_gap_k=self.config.ocr.cluster_x_gap_k,
            x_gap_min_px=self.config.ocr.cluster_x_gap_min_px,
        )

        # ── 空間トラッキング: 配列インデックスではなく永続IDを割り当てる ────────
        # _cluster_tracker はフレーム間の記憶を持つ OcrWorker インスタンスの
        # 状態であり、raw_clusters の出現順序が変わっても、空間的に同じ
        # 位置にあるクラスターには同じ cluster_id が一貫して割り当てられる。
        persistent_ids = self._cluster_tracker.assign(raw_clusters)

        results: list[OcrResult] = []
        for cid, cluster_lines in zip(persistent_ids, raw_clusters):
            full_text = "\n".join(lr.text for lr in cluster_lines)
            results.append(OcrResult(
                full_text=full_text,
                lines=cluster_lines,
                capture_left=capture_left,
                capture_top=capture_top,
                cluster_id=cid,
            ))

        return results


# ─────────────────────────────────────────────
#  モジュールレベルのヘルパー関数（スタンドアロンテストでも共有）
# ─────────────────────────────────────────────

def _plurality_vote(votes: deque) -> str:
    """
    deque に格納された直近 N フレーム分の OCR テキストに対して
    多数決（plurality voting）を行い、最も出現回数の多いテキストを返す。

    ── 用途 ─────────────────────────────────────────────────────────────────
    ゲームの背景アニメーションなどで OCR が1〜2フレームだけ大きく誤読した
    場合（例: "Sets the starting" → "Darting ValLle Of"）に、
    正しいテキストが多数を占める限り誤読を無視して翻訳キャッシュヒットを
    維持することで、字幕のチラつき・再翻訳を防ぐ。

    ── タイブレーク ─────────────────────────────────────────────────────────
    複数のテキストが同一の最大得票数を持つ場合は、deque の右端（最新フレーム）
    から走査して、最初に見つかった候補を採用する。

    「最新優先」を選ぶ根拠: 全フレームが異なる読みになるほどの激しいノイズ
    時は、最も新しいフレームのテキストが「ノイズが落ち着き始めた瞬間」に
    最も近い可能性が高い。旧フレームよりも直近を優先することで、
    画面テキストの変化（例: スコア 7→8）もできるだけ早く追従できる。

    ── N=1 の動作 ─────────────────────────────────────────────────────────
    majority_vote_frames=1（多数決無効）の場合、deque には常に1件のみが
    入り、無条件でその1件が返される。既存コードと完全に同等の動作になる。

    Args:
        votes: deque[str]。OcrWorker 内で maxlen=majority_vote_frames として
               管理される。空呼び出しは想定しないが空でも安全に "" を返す。

    Returns:
        多数決の勝者テキスト文字列。
    """
    if not votes:
        return ""
    if len(votes) == 1:
        return votes[-1]

    counter = Counter(votes)
    max_count = max(counter.values())

    # 同票候補が1つだけなら確定
    top_candidates = {text for text, cnt in counter.items() if cnt == max_count}
    if len(top_candidates) == 1:
        return top_candidates.pop()

    # タイ: deque 右端（最新）から走査して最初に見つかった候補を採用
    for text in reversed(votes):
        if text in top_candidates:
            return text

    return votes[-1]  # 到達しないはずだが安全のため


def _natural_break_groups(
    spans: list[tuple[float, float]],
    k: float = 3.0,
    min_gap_px: float = 20.0,
) -> list[list[int]]:
    """
    (start, end) のスパン群を「統計的ギャップ検出（自然ブレーク検出）」で
    グループ分割する統一ヘルパー。

    単語レベルの強制X分割（_do_ocr）と行レベルの列分割（_cluster_lines）の
    両方から呼ばれ、同一のロジックを共有する。

    ── 設計思想 ─────────────────────────────────────────────────────────────────
    固定の絶対値・固定の倍率（フォント高さ × N など）を閾値にすると、
    フォントサイズやUIデザインが変わるたびに調整が必要になる。

    代わりに「そのフレームのテキスト配置が自分自身で持っている統計的特徴」を
    閾値として使う: 隣接スパン間のギャップを全て計算し、その中央値を
    「典型的な（同一グループ内の）間隔」とみなす。中央値は外れ値の影響を
    受けにくいため、少数の「パネル境界の異常に大きなギャップ」が紛れ込んでも
    "典型的な間隔" の推定値は歪まない。

    例:
        スパン間ギャップ = [40, 40, 40, 40, 540, 40, 40, 40]
        median(ギャップ) = 40
        threshold = max(40 × k, min_gap_px)  例: k=3 → 120
        540 > 120 → ここを境界として分割

    ── サンプル数が少ない場合のフォールバック ─────────────────────────────────
    ギャップのサンプル数が1個以下では中央値が統計的に意味を持たないため、
    min_gap_px をそのまま閾値として使う（安全側に倒す = 分割しにくくする）。

    Args:
        spans:      (start, end) のタプルのリスト。事前ソート不要。
        k:          中央値ギャップに対する倍率。これを超えるギャップを境界とみなす。
        min_gap_px: ギャップ閾値の最低保証値（px）。

    Returns:
        グループのリスト。各グループは spans への元のインデックスのリスト
        （グループ内は start 順）。入力が空なら空リストを返す。
    """
    n = len(spans)
    if n == 0:
        return []
    if n == 1:
        return [[0]]

    # start位置でソートした順序（インデックス参照は元のリストのまま）
    order = sorted(range(n), key=lambda i: spans[i][0])

    gaps = [
        spans[order[i]][0] - spans[order[i - 1]][1]
        for i in range(1, n)
    ]

    if len(gaps) < 2:
        # ギャップが1個だけでは中央値が信頼できないため固定フォールバックを使う
        threshold = min_gap_px
    else:
        sorted_gaps = sorted(gaps)
        median_gap = sorted_gaps[len(sorted_gaps) // 2]
        threshold = max(median_gap * k, min_gap_px)

    groups: list[list[int]] = [[order[0]]]
    for pos in range(1, n):
        idx = order[pos]
        prev_idx = order[pos - 1]
        gap = spans[idx][0] - spans[prev_idx][1]
        if gap > threshold:
            groups.append([idx])
        else:
            groups[-1].append(idx)

    return groups


def _cluster_lines(
    lines: list[LineRect],
    y_scale: float = 1.0,
    min_y_px: float = 15.0,
    x_gap_k: float = 3.0,
    x_gap_min_px: float = 40.0,
) -> list[list[LineRect]]:
    """
    行リストを2Dギャップ検出でクラスタリングする。

    ── Y軸バンド分割（変更なし）─────────────────────────────────────────────
    引き続き「検出行の median(高さ) の y_scale 倍」を閾値とする方式を使う。
    フォントの代表高さ unit_h は IQR フィルタ付き中央値で安定的に推定できる。

    ── X軸列分割（統計的ギャップ検出に刷新）───────────────────────────────────
    旧: unit_h × x_scale という「フォント高さからの類推」を閾値にしていた。
        → フォント高さと水平方向のUIレイアウト間隔は本質的に独立した値であり、
          高さベースの推定が実際の列間隔とたまたま近いと、わずかな誤差で
          分割に失敗するケースがあった（例: 閾値94px vs 実測ギャップ90px）。
    新: _natural_break_groups() による「そのバンド自身が持つギャップ分布の
        中央値」を基準にする。UIのフォントサイズやレイアウトに依存せず、
        そのフレームのテキスト密度から動的に閾値を導出するため頑健。

    Args:
        lines:        フィルタ済みの LineRect リスト
        y_scale:      Y方向ギャップ = y_scale × unit_h（config.ocr.cluster_y_scale）
        min_y_px:     y_gap_px の最低保証値（px）
        x_gap_k:      X方向: 中央値ギャップに対する倍率（config.ocr.cluster_x_gap_k）
        x_gap_min_px: X方向ギャップ閾値の最低保証値（config.ocr.cluster_x_gap_min_px）

    Returns:
        クラスターのリスト。各クラスターは LineRect のリスト（Y順）。
    """
    if not lines:
        return []

    # ── unit_h: フォント代表高さを IQR フィルタ付き中央値で計算（Y軸用、変更なし）
    heights = sorted(lr.local_h for lr in lines)
    n = len(heights)
    if n >= 4:
        q1 = heights[n // 4]
        q3 = heights[3 * n // 4]
        iqr = q3 - q1
        valid_h = [h for h in heights if q1 - 1.5 * iqr <= h <= q3 + 1.5 * iqr]
    else:
        valid_h = heights
    if not valid_h:
        valid_h = heights
    unit_h = max(valid_h[len(valid_h) // 2], 1)  # ゼロ除算防止

    y_gap_px = max(y_scale * unit_h, min_y_px)

    logger.debug(
        "_cluster_lines: unit_h=%dpx  y_gap=%.0fpx  x_gap_k=%.1f  lines=%d",
        unit_h, y_gap_px, x_gap_k, len(lines),
    )

    # ── Step 1: Y軸バンド分割（変更なし） ───────────────────────────────────
    sorted_by_y = sorted(lines, key=lambda lr: lr.local_y + lr.local_h // 2)
    y_bands: list[list[LineRect]] = [[sorted_by_y[0]]]
    for line in sorted_by_y[1:]:
        prev        = y_bands[-1][-1]
        prev_bottom = prev.local_y + prev.local_h
        y_gap       = line.local_y - prev_bottom
        if y_gap > y_gap_px:
            y_bands.append([line])
        else:
            y_bands[-1].append(line)

    # ── Step 2: 各バンド内をX軸で統計的ギャップ検出により列分割（刷新） ──────
    clusters: list[list[LineRect]] = []
    for band in y_bands:
        if len(band) == 1:
            clusters.append(band)
            continue

        spans = [(lr.local_x, lr.local_x + lr.local_w) for lr in band]
        groups_idx = _natural_break_groups(spans, k=x_gap_k, min_gap_px=x_gap_min_px)
        for idx_list in groups_idx:
            clusters.append([band[i] for i in idx_list])

    # ── Step 3: 各クラスター内をY座標でソート（読み順） ─────────────────────
    for cluster in clusters:
        cluster.sort(key=lambda lr: lr.local_y)

    return clusters


def _apply_self_mask(
    img: Image.Image,
    mask_regions: list[tuple[int, int, int, int]],
    capture_left: int,
    capture_top: int,
) -> Image.Image:
    """
    キャプチャ画像内の指定領域（自分自身が描画した字幕）を黒で塗りつぶす。

    mask_regions は画面絶対座標（物理ピクセル）のリスト [(x, y, w, h), ...]。
    capture_left/top を引いてキャプチャ画像内のローカル座標に変換し、
    キャプチャ範囲外にはみ出す部分はクリップする。

    塗りつぶし色を黒にする理由:
        OCR が文字を検出しなくなるだけでよく、特定の色である必要はない。
        黒で塗ると _is_meaningful_line のノイズフィルタにも確実に
        引っかからない（英数字比率0%で除外される）。

    Args:
        img:          mss でキャプチャした PIL.Image（RGB）
        mask_regions: MaskRegionStore.get() で取得した矩形リスト
        capture_left: キャプチャ領域の画面上での左端X（物理ピクセル）
        capture_top:  キャプチャ領域の画面上での上端Y（物理ピクセル）

    Returns:
        マスク適用済みの新しい PIL.Image（元の img は変更しない）
    """
    masked = img.copy()
    draw = ImageDraw.Draw(masked)

    for mx, my, mw, mh in mask_regions:
        # 画面絶対座標 → キャプチャ画像内ローカル座標
        local_x = mx - capture_left
        local_y = my - capture_top

        # キャプチャ範囲との交差部分のみを塗りつぶす（範囲外は無視）
        x0 = max(0, local_x)
        y0 = max(0, local_y)
        x1 = min(masked.width,  local_x + mw)
        y1 = min(masked.height, local_y + mh)

        if x1 > x0 and y1 > y0:
            draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=(0, 0, 0))

    return masked


def _is_meaningful_line(text: str, min_chars: int = 3) -> bool:
    """
    OCR結果の1行が「意味のあるテキスト」かどうかを判定するフィルタ。

    除外対象:
      - 3文字未満の断片（UIアイコン・ノイズ）
      - 英数字の比率が40%未満（記号・罫線ノイズ）
      - 使われている文字の種類が2種類以下（'|||||||' や '.......' など繰り返しノイズ）
      - 単独の記号文字のみ
    """
    stripped = text.strip()

    # ① 最小文字数
    if len(stripped) < min_chars:
        return False

    # ② 英数字の比率（英語 OCR を前提とした最低限のチェック）
    alnum_count = sum(1 for c in stripped if c.isalnum())
    if alnum_count / len(stripped) < 0.4:
        return False

    # ③ ユニーク文字種が少なすぎる（繰り返しノイズ）
    unique_non_space = set(stripped.replace(" ", ""))
    if len(unique_non_space) <= 2:
        return False

    # ④ 英字が1文字も含まれないが数字でもない（pure symbol）
    has_letter = any(c.isalpha() for c in stripped)
    has_digit  = any(c.isdigit() for c in stripped)
    if not has_letter and not has_digit:
        return False

    return True


def _find_window_rect(keyword: str) -> Optional[tuple[int, int, int, int]]:
    """
    タイトルにキーワードを含むウィンドウを探して (x, y, width, height) を返す。
    最小化されているウィンドウは除外する。見つからない場合は None。
    """
    try:
        import pygetwindow as gw
        matches = gw.getWindowsWithTitle(keyword)
        if not matches:
            return None
        win = matches[0]
        if win.width <= 0 or win.height <= 0:
            logger.warning("ウィンドウ '%s' は最小化されています", keyword)
            return None
        return (win.left, win.top, win.width, win.height)
    except Exception as exc:
        logger.error("pygetwindow エラー: %s", exc)
        return None


async def _pil_to_software_bitmap(img: Image.Image) -> SoftwareBitmap:
    """
    PIL.Image → winsdk SoftwareBitmap (BGRA8 / Premultiplied) へ変換する。

    BitmapDecoder 経由にすることで、WinRT 側がフォーマット変換を肩代わりしてくれる。
    PNG エンコード → InMemoryRandomAccessStream → BitmapDecoder → SoftwareBitmap の流れ。
    """
    # RGBA に統一してから PNG バイト列に変換
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG")
    png_bytes = bytearray(buf.getvalue())

    # InMemoryRandomAccessStream に書き込む
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(png_bytes)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    # BitmapDecoder でデコード → SoftwareBitmap 取得
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    # OcrEngine が要求するフォーマット (BGRA8 + Premultiplied) に変換
    if (bitmap.bitmap_pixel_format != BitmapPixelFormat.BGRA8
            or bitmap.bitmap_alpha_mode != BitmapAlphaMode.PREMULTIPLIED):
        bitmap = SoftwareBitmap.convert(
            bitmap,
            BitmapPixelFormat.BGRA8,
            BitmapAlphaMode.PREMULTIPLIED,
        )

    return bitmap


def _text_similarity(a: str, b: str) -> float:
    """2つの文字列の類似度を 0.0〜1.0 で返す"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _save_capture_debug(img: Image.Image) -> None:
    """debug.save_captures=true のとき captures/ にキャプチャ画像を保存する"""
    save_dir = Path(__file__).parent / "captures"
    save_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%H%M%S_%f")
    path = save_dir / f"capture_{ts}.png"
    img.save(path)
    logger.debug("キャプチャ保存: %s", path)


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

    async def _run_tests(cfg: AppConfig) -> None:
        print(f"\n{SEP}")
        print("  ocr_engine.py  スタンドアロン動作確認")
        print(SEP)

        # ────────────────────────────────
        # STEP 1: ウィンドウ検索
        # ────────────────────────────────
        keyword = cfg.target_window.window_title_keyword
        print(f"\n[STEP 1] ウィンドウ検索  キーワード='{keyword}'")

        rect = _find_window_rect(keyword)
        if rect:
            print(f"  ✓ 検出: x={rect[0]}, y={rect[1]}, "
                  f"width={rect[2]}, height={rect[3]}")
        else:
            print(f"  ✗ ウィンドウが見つかりません。")
            print(f"  → プライマリモニター全体をフォールバックとして使用します。")
            with mss.mss() as sct:
                m = sct.monitors[1]  # monitors[0] は全モニター合成領域
                rect = (m["left"], m["top"], m["width"], m["height"])
            print(f"  → フォールバック矩形: {rect}")

        # ────────────────────────────────
        # STEP 2: スクリーンキャプチャ
        # ────────────────────────────────
        print(f"\n[STEP 2] スクリーンキャプチャ")
        wx, wy, ww, wh = rect
        crop = cfg.capture.crop_region

        if crop is not None:
            left   = wx + int(ww * crop.left_ratio)
            top    = wy + int(wh * crop.top_ratio)
            right  = wx + int(ww * crop.right_ratio)
            bottom = wy + int(wh * crop.bottom_ratio)
            print(f"  crop_region 適用: top_ratio={crop.top_ratio} "
                  f"→ キャプチャ上端 y={top}")
        else:
            left, top, right, bottom = wx, wy, wx + ww, wy + wh
            print(f"  crop_region=null: ウィンドウ全体をキャプチャ")

        monitor = {
            "left": left, "top": top,
            "width": right - left, "height": bottom - top,
        }

        try:
            with mss.mss() as sct:
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
            print(f"  ✓ キャプチャ成功: {img.size[0]} x {img.size[1]} px")
        except Exception as e:
            print(f"  ✗ キャプチャ失敗: {e}")
            sys.exit(1)

        # ────────────────────────────────
        # STEP 3: OCRエンジン初期化確認
        # ────────────────────────────────
        print(f"\n[STEP 3] OCRエンジン初期化  language='{cfg.ocr.language}'")
        language = Language(cfg.ocr.language)
        engine = OcrEngine.try_create_from_language(language)

        if engine is None:
            print(f"  ✗ エンジン作成失敗。")
            print(f"  → Windows の設定 > 時刻と言語 > 言語 で '{cfg.ocr.language}' の")
            print(f"    言語パック（OCR機能を含む）がインストールされているか確認してください。")
            sys.exit(1)
        else:
            print(f"  ✓ エンジン作成成功")
            print(f"    最大認識テキスト長: {engine.max_image_dimension} px")

        # ────────────────────────────────
        # STEP 4: 実際のOCR認識
        # ────────────────────────────────
        print(f"\n[STEP 4] OCR認識実行")
        try:
            bitmap = await _pil_to_software_bitmap(img)
            print(f"  ✓ SoftwareBitmap 変換成功: "
                  f"{bitmap.pixel_width}x{bitmap.pixel_height} px")

            ocr_result = await engine.recognize_async(bitmap)

            full_text = ocr_result.text.strip()
            print(f"  ✓ 認識成功")
            print(f"\n  ── 認識テキスト全文 {'─'*30}")
            if full_text:
                for line_text in full_text.splitlines():
                    print(f"    {line_text}")
            else:
                print("    (テキストが検出されませんでした)")
            print(f"  {'─'*45}")

            print(f"\n  ── ライン別詳細（先頭5件）{'─'*20}")
            for i, line in enumerate(ocr_result.lines[:5]):
                words = " ".join(w.text for w in line.words)
                # OcrLine には bounding_rect がないため OcrWord を集約して計算
                wwr = [w for w in line.words
                       if hasattr(w, "bounding_rect") and w.bounding_rect is not None]
                if wwr:
                    lx = int(min(w.bounding_rect.x                          for w in wwr))
                    ly = int(min(w.bounding_rect.y                          for w in wwr))
                    rx = int(max(w.bounding_rect.x + w.bounding_rect.width  for w in wwr))
                    by = int(max(w.bounding_rect.y + w.bounding_rect.height for w in wwr))
                    rect_str = f"x={lx}, y={ly}, w={rx - lx}, h={by - ly}"
                else:
                    rect_str = "N/A (bounding_rect 取得不可)"
                print(f"    Line {i+1}: '{words}'")
                print(f"           BoundingRect: {rect_str}")

        except Exception as e:
            print(f"  ✗ OCR処理失敗: {e}")
            logger.exception("OCR実行エラー詳細")
            sys.exit(1)

        # ────────────────────────────────
        # STEP 5: 差分検知ロジック確認
        # ────────────────────────────────
        print(f"\n[STEP 5] 差分検知ロジック確認  threshold={cfg.ocr.similarity_threshold}")
        text_a = "Hello, this is a test sentence."
        text_b = "Hello, this is a test sentence."   # 完全一致
        text_c = "Hello, this is completely different."
        sim_ab = _text_similarity(text_a, text_b)
        sim_ac = _text_similarity(text_a, text_c)
        threshold = cfg.ocr.similarity_threshold
        print(f"  A vs B (同一):   類似度={sim_ab:.3f}  "
              f"{'→ スキップ ✓' if sim_ab >= threshold else '→ 翻訳実行'}")
        print(f"  A vs C (異なる): 類似度={sim_ac:.3f}  "
              f"{'→ スキップ' if sim_ac >= threshold else '→ 翻訳実行 ✓'}")

        print(f"\n{SEP}")
        print("  全ステップ完了")
        print(SEP + "\n")

    cfg = load_config()
    asyncio.run(_run_tests(cfg))

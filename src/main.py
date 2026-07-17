"""
main.py  ― Dolphin リアルタイム翻訳オーバーレイ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Part 1: OverlayWindow        透過・クリックスルー・OBS 対応ウィンドウ
Part 2: SubtitleLabel        個別字幕ボックス（QPainterPath 縁取り描画）
        SubtitleManager      字幕の動的生成・座標追従・寿命管理
Part 3: StatusBar            デバッグ用ステータス表示
        MainController       ワーカー統括・シグナル配線・シャットダウン
Part 4: SettingsDialog       起動時ランチャーGUI（非エンジニア向け設定変更）
        SystemTrayIcon       タスクトレイ常駐・右クリックメニュー制御
Part 5: _setup_logging()     ロギング設定
        _install_excepthook() 未捕捉例外フック
        main()               エントリポイント
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【v2 変更点 — ゾンビ現象・ミルフィーユ現象の根治】

  1. 世代管理 (Generation ID)
       SubtitleManager がクラスターごとに世代番号 (_generation) を管理する。
       翻訳リクエスト送出時に世代番号を OcrResult.generation へ書き込み、
       translation_ready コールバック受信時に照合する。
       クリア済みクラスターの古い翻訳結果は無条件で破棄されるため、
       「翻訳レイテンシ中にクラスターが消滅 → 翻訳完了で復活」が起きない。

  2. オブジェクトプール廃止・deleteLater() 徹底
       _pool を完全に削除。ラベルは SubtitleGroup が所有し、
       不要になった時点で必ず deleteLater() を呼ぶ。
       ウィジェットの所有権が常に明確なため、hide() 漏れによる
       ミルフィーユ（古い行数のラベルが残存）が原理的に起きない。

  3. パブリック API の整理
       _clear_group → clear_group (パブリック化)
       MainController は _clear_group の内部実装に依存しなくなった。
       空テキスト判定の責務を MainController._on_text_detected に集約し、
       SubtitleManager.update_subtitles はテキストが存在する前提で動く。

【v3 変更点 — 非エンジニア向け配布（ベータ配布）対応】

  1. ログローテーション
       5MB × 3世代に変更（_setup_logging）。

  2. 起動時ランチャーGUI (SettingsDialog)
       config の状態に関わらず、起動時は必ず SettingsDialog を表示する。
       「保存して起動」でのみメイン処理へ進み、「キャンセル」「×」では
       アプリを終了する。保存時は config.json の _comment キー等を
       壊さないよう、対象キーだけを部分的に差し替えて書き戻す
       （config.py の save_partial_config を参照）。

  3. タスクトレイ (SystemTrayIcon)
       [設定] [一時停止/再開] [翻訳キャッシュをクリア] [終了] を提供する。
       実際のスレッド制御（asyncio.Event の call_soon_threadsafe 経由操作、
       TranslationCache の安全なクリア）は MainController のパブリック
       メソッド（pause / resume / clear_translation_cache）に閉じ込め、
       SystemTrayIcon 自身はスレッドの存在を意識しない。
       一時停止時は OcrWorker のサスペンドと同時に、画面に残っている
       字幕も SubtitleManager.clear_all() で消去する
       （ムービー中などに古い字幕が残存して見えるのを防ぐ）。
"""

from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════════
#  標準ライブラリ
# ══════════════════════════════════════════════════════════════════════════════
import ctypes
import ctypes.wintypes
import logging
import logging.handlers
import sys
import traceback
from pathlib import Path
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
#  PyQt6
# ══════════════════════════════════════════════════════════════════════════════
from PyQt6.QtCore import (
    QObject,
    QPoint,
    QRect,
    Qt,
    QTimer,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QScreen,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QWidget,
)

# ══════════════════════════════════════════════════════════════════════════════
#  自プロジェクト
# ══════════════════════════════════════════════════════════════════════════════
from config import APP_DIR, AppConfig, OverlayConfig, load_config, save_partial_config
from ocr_engine import LineRect, MaskRegionStore, OcrResult, OcrWorker
from translator import TranslatorWorker

logger = logging.getLogger(__name__)
# ─────────────────────────────────────────────────────────────────────────────
#  ログファイルのパス（exe の隣 / 開発時はスクリプトの隣）
#  ※ パス解決ロジックの詳細コメントは config.py の _get_app_dir() を参照。
# ─────────────────────────────────────────────────────────────────────────────
_LOG_FILE = APP_DIR / "overlay.log"


# ══════════════════════════════════════════════════════════════════════════════
#  Part 1  ―  Win32 クリックスルー ／ OverlayWindow
# ══════════════════════════════════════════════════════════════════════════════

_GWL_EXSTYLE:     int = -20
_WS_EX_LAYERED:   int = 0x0008_0000
_WS_EX_TRANSPARENT: int = 0x0000_0020
_WS_EX_NOACTIVATE:  int = 0x0800_0000
_WS_EX_TOOLWINDOW:  int = 0x0000_0080

def _apply_click_through(hwnd: int) -> None:
    """Win32 API でウィンドウをクリックスルー可能なレイヤードウィンドウに変換する"""
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        current = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
        new_style = (
            current
            | _WS_EX_LAYERED
            | _WS_EX_TRANSPARENT
            | _WS_EX_NOACTIVATE
            | _WS_EX_TOOLWINDOW
        )
        user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, new_style)
        logger.debug(
            "_apply_click_through: HWND=0x%X  ExStyle 0x%X → 0x%X",
            hwnd, current, new_style,
        )
    except AttributeError:
        logger.warning("_apply_click_through: Windows 以外の OS では無効化されます")
    except OSError as exc:
        logger.error("_apply_click_through: Win32 API エラー: %s", exc)


class OverlayWindow(QWidget):
    """
    モニター全体を覆う完全透明な「幽霊ウィンドウ」。
    自身はクリックスルーで、SubtitleLabel の親コンテナとして機能する。
    OBS の「ウィンドウキャプチャ」でアルファチャンネルが維持される。
    """

    def __init__(self, config: AppConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = config
        self._click_through_applied = False
        self._setup_window_flags()
        self._setup_geometry()

    def _setup_window_flags(self) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _setup_geometry(self) -> None:
        screen: QScreen = QApplication.primaryScreen()
        full_rect: QRect = screen.geometry()
        self.setGeometry(full_rect)
        logger.info(
            "OverlayWindow ジオメトリ: (%d, %d) %dx%d",
            full_rect.x(), full_rect.y(), full_rect.width(), full_rect.height(),
        )

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 0))
        painter.end()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if not self._click_through_applied:
            hwnd = int(self.winId())
            _apply_click_through(hwnd)
            self._click_through_applied = True
            logger.info("OverlayWindow: Win32 クリックスルー適用完了 (HWND=0x%X)", hwnd)

    def relocate_to_screen(self, phys_x: int, phys_y: int) -> None:
        """
        物理ピクセル座標 (phys_x, phys_y) を含むモニターにオーバーレイを移動する。

        【実装方針】
        showFullScreen() + setScreen() を使わず、showNormal() → setGeometry() → show()
        の順序で位置とサイズを直接指定する。

        理由:
          Windows PyQt6 では showFullScreen() を呼ぶと Qt が「フルスクリーン状態」を
          内部管理し始め、以降 setGeometry() や setScreen() による移動を無視する。
          showNormal() で一旦通常ウィンドウ状態に戻してから setGeometry() を呼ぶことで、
          Qt の座標管理を完全に上書きできる。

          また showNormal() / show() などのウィンドウ状態遷移時に Qt が内部で
          SetWindowLongPtrW を呼び出し WS_EX_TRANSPARENT 等がリセットされることがある。
          そのため _apply_click_through() は状態遷移後に必ず再適用する。

        呼び出しタイミング: OCR がテキストを検知するたびに MainController から呼ぶ。
        スクリーンが変わっていない場合は早期リターン（コスト無し）。
        """
        dpr: float = self.devicePixelRatioF()
        # 物理座標 → Qt 論理座標に変換してスクリーンを特定
        logical_pt = QPoint(round(phys_x / dpr), round(phys_y / dpr))
        target_screen: Optional[QScreen] = QApplication.screenAt(logical_pt)
        if target_screen is None:
            target_screen = QApplication.primaryScreen()

        # 既に正しいスクリーンにある場合はスキップ（毎 OCR フレームで呼ばれるため重要）
        if self.screen() == target_screen:
            return

        new_rect: QRect = target_screen.geometry()
        logger.info(
            "OverlayWindow をスクリーン '%s' へ移動: (%d, %d) %dx%d",
            target_screen.name(),
            new_rect.x(), new_rect.y(), new_rect.width(), new_rect.height(),
        )

        # ① showFullScreen() が設定した Qt フルスクリーン状態を解除する
        #    これにより以降の setGeometry() が有効になる
        self.showNormal()

        # ② 対象スクリーンの論理矩形にウィンドウの位置・サイズを一致させる
        #    setFixedSize(new_rect.size()) + move(new_rect.topLeft()) でも同等
        self.setGeometry(new_rect)

        # ③ 再表示・最前面化
        self.show()
        self.raise_()

        # ④ Win32 クリックスルーフラグを必ず再適用する
        #    showNormal() の内部処理で Qt が拡張ウィンドウスタイルをリセットすることがある
        _apply_click_through(int(self.winId()))
        logger.info("OverlayWindow: Win32 クリックスルーフラグ再適用完了")


# ══════════════════════════════════════════════════════════════════════════════
#  Part 2  ―  SubtitleLabel ／ SubtitleManager
# ══════════════════════════════════════════════════════════════════════════════

def _parse_argb_color(color_str: str) -> QColor:
    """'#AARRGGBB' / '#RRGGBB' 形式の文字列を QColor に変換する"""
    s = color_str.lstrip("#")
    try:
        if len(s) == 8:
            a, r, g, b = (int(s[i:i+2], 16) for i in (0, 2, 4, 6))
        elif len(s) == 6:
            r, g, b = (int(s[i:i+2], 16) for i in (0, 2, 4))
            a = 255
        else:
            raise ValueError(f"未対応の色フォーマット: '{color_str}'")
        return QColor(r, g, b, a)
    except (ValueError, IndexError) as exc:
        logger.warning("色パースに失敗しました (%s)。白色へフォールバック。", exc)
        return QColor(255, 255, 255, 255)


class SubtitleLabel(QWidget):
    """
    1 行分の翻訳テキストを表示する半透明の字幕ボックス。
    QPainterPath による縁取り描画で、あらゆる背景色に対して視認性を保証する。
    """

    _OUTLINE_WIDTH: float = 1.8

    def __init__(self, cfg: OverlayConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._bg_color: QColor = _parse_argb_color(cfg.background_color)
        self._fg_color: QColor = _parse_argb_color(cfg.font_color)
        self._outline_color: QColor = QColor(0, 0, 0, min(255, self._fg_color.alpha() + 80))
        self._pad_x: int = cfg.padding_x_px
        self._pad_y: int = cfg.padding_y_px

        self._font = QFont(cfg.font_family, cfg.font_size_pt)
        self._font.setWeight(QFont.Weight.Medium)
        self._metrics = QFontMetrics(self._font)
        self._text: str = ""
        self.hide()

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self._recalc_size()
        self.update()

    def get_text(self) -> str:
        return self._text

    def _recalc_size(self) -> None:
        if not self._text:
            self._wrapped_lines: list[str] = []
            self.resize(0, 0)
            return

        # 親ウィンドウ幅の90%を折り返しの上限にする
        parent = self.parentWidget()
        max_content_w = int(parent.width() * 0.9) if parent else 1400
        max_content_w = max(
            max_content_w - self._pad_x * 2 - int(self._OUTLINE_WIDTH * 2), 80
        )

        self._wrapped_lines = self._compute_wrapped_lines(self._text, max_content_w)

        actual_w = max(self._metrics.horizontalAdvance(ln) for ln in self._wrapped_lines)
        actual_h = self._metrics.height() * len(self._wrapped_lines)

        w = actual_w + self._pad_x * 2 + int(self._OUTLINE_WIDTH * 2)
        h = actual_h + self._pad_y * 2 + int(self._OUTLINE_WIDTH * 2)
        self.resize(w, h)

    def _compute_wrapped_lines(self, text: str, max_width: int) -> list[str]:
        """
        単語境界でテキストを折り返し、行リストを返す。
        LLM が出力した既存の改行も段落の区切りとして尊重する。
        """
        result: list[str] = []
        for paragraph in text.splitlines():
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            current = ""
            for word in paragraph.split():
                candidate = (current + " " + word).lstrip() if current else word
                if self._metrics.horizontalAdvance(candidate) <= max_width:
                    current = candidate
                else:
                    if current:
                        result.append(current)
                    current = word
            if current:
                result.append(current)
        return result if result else [text]

    def paintEvent(self, event) -> None:  # noqa: ANN001
        lines = getattr(self, "_wrapped_lines", None)
        if not self._text or not lines:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        # 半透明座布団（背景矩形）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._bg_color)
        painter.drawRoundedRect(self.rect(), 4.0, 4.0)

        painter.setFont(self._font)
        outline_pen = QPen(self._outline_color)
        outline_pen.setWidthF(self._OUTLINE_WIDTH * 2)
        outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

        text_x = float(self._pad_x + int(self._OUTLINE_WIDTH))
        line_h  = self._metrics.height()

        for i, line in enumerate(lines):
            # addText の y 引数はベースライン（ascent 分下げる）
            baseline_y = float(
                self._pad_y + int(self._OUTLINE_WIDTH)
                + i * line_h
                + self._metrics.ascent()
            )
            path = QPainterPath()
            path.addText(text_x, baseline_y, self._font, line)
            # 縁取り（黒）→ 本文字（前景色）の順で重ね描き
            painter.strokePath(path, outline_pen)
            painter.fillPath(path, self._fg_color)

        painter.end()


class SubtitleManager:
    """
    OverlayWindow 上の SubtitleLabel 群を管理するコントローラー。

    【v2 設計方針】
    ─ 世代管理 (Generation ID) ─────────────────────────────────────────────
    クラスターごとに単調増加する世代番号 (_generation) を持つ。
    翻訳リクエスト送出時に現世代番号を ocr_result.generation へ書き込み、
    translation_ready 受信時に照合する。
    clear_group() は世代番号を削除することで「このクラスターの全翻訳結果は
    古い」と宣言する。これによりゾンビ復活が原理的に起きない。

    ─ オブジェクトプール廃止 ───────────────────────────────────────────────
    旧実装の _pool は「誰がラベルを所有しているか」が実行時にしか分からず、
    hide() 漏れ・サイズ不整合の温床だった。
    v2 では SubtitleLabel を _SubtitleGroup が1対1で所有する。
    不要になったラベルは必ず deleteLater() で Qt のイベントループに破棄を
    委ね、親子ウィジェットツリーからも確実に取り除く。

    ─ パブリック API ────────────────────────────────────────────────────────
    clear_group()    : 指定クラスターの字幕を消去（外部公開）
    clear_all()      : 全クラスターを一括消去
    update_subtitles(): テキストと座標を受け取って字幕を描画・更新
    stamp_generation(): 翻訳リクエスト送出直前に呼び、世代番号を OcrResult へ
                        書き込んで返す（MainController から呼ぶ）
    """

    # ── 内部データクラス ────────────────────────────────────────────────────
    class _SubtitleGroup:
        """
        1 クラスター分の SubtitleLabel 群を所有する内部コンテナ。
        ラベルの破棄責務をここに集約することでライフサイクルを明確化する。
        """
        __slots__ = ("labels",)

        def __init__(self) -> None:
            self.labels: list[SubtitleLabel] = []

        def dispose(self) -> None:
            """全ラベルを Qt イベントループ経由で安全に破棄する"""
            for label in self.labels:
                label.hide()
                label.deleteLater()
            self.labels.clear()

    # ── 初期化 ───────────────────────────────────────────────────────────────

    def __init__(
        self,
        parent: QWidget,
        cfg: OverlayConfig,
        mask_store: Optional[MaskRegionStore] = None,
    ) -> None:
        self._parent = parent
        self._cfg = cfg
        self._offset_y: int = cfg.popup_offset_y_px
        self._duration_ms: int = cfg.display_duration_sec * 1000

        # cluster_id → _SubtitleGroup（ラベルの所有者）
        self._groups: dict[int, SubtitleManager._SubtitleGroup] = {}

        # cluster_id → 現在の世代番号
        # clear_group() 時に削除することで「旧世代は全て無効」を表現する
        self._generation: dict[int, int] = {}

        # 自己マスキング用の共有ストア（OcrWorker と共有）
        self._mask_store = mask_store

        self._expire_timer = QTimer(parent)
        self._expire_timer.setSingleShot(True)
        self._expire_timer.timeout.connect(self.clear_all)

    # ── パブリック API ────────────────────────────────────────────────────────

    def stamp_generation(self, ocr_result: OcrResult) -> OcrResult:
        """
        翻訳リクエスト送出直前に呼ぶ。

        クラスターの世代番号をインクリメントし、ocr_result.generation に
        書き込んで返す。MainController は返値の OcrResult を
        TranslatorWorker.request_translation() へ渡す。

        世代番号は同一 cluster_id の翻訳リクエストが重複した場合に
        古いものを識別するためのタグとして機能する。
        """
        cid = ocr_result.cluster_id
        new_gen = self._generation.get(cid, 0) + 1
        self._generation[cid] = new_gen
        # OcrResult はデータクラスなので generation フィールドを直接更新する
        ocr_result.generation = new_gen
        logger.debug("世代番号発行: cluster=%d  gen=%d", cid, new_gen)
        return ocr_result

    def update_subtitles(self, translated_text: str, ocr_result: OcrResult) -> None:
        """
        翻訳完了シグナルを受けて字幕を描画・更新する。

        世代チェックを最初に行い、クリア済みクラスターや後発リクエストに
        追い越されたクラスターの翻訳結果は無条件で破棄する。

        Args:
            translated_text: 翻訳済みテキスト（空文字の場合はクリア）
            ocr_result:      generation フィールドが stamp_generation() で
                             書き込まれた OcrResult
        """
        cluster_id = ocr_result.cluster_id
        incoming_gen = getattr(ocr_result, "generation", None)

        # ── 世代チェック ────────────────────────────────────────────────────
        # _generation に cluster_id のエントリがない = clear_group() 済み
        # 現世代と一致しない = より新しいリクエストが既に発行されている
        current_gen = self._generation.get(cluster_id)
        if current_gen is None:
            logger.debug(
                "世代チェック: cluster=%d は消滅済み（gen=%s）。翻訳結果を破棄。",
                cluster_id, incoming_gen,
            )
            return
        if incoming_gen is not None and incoming_gen != current_gen:
            logger.debug(
                "世代チェック: cluster=%d gen=%s は古い（現在=%d）。翻訳結果を破棄。",
                cluster_id, incoming_gen, current_gen,
            )
            return

        # ── 空テキストはクリア扱い ──────────────────────────────────────────
        if not translated_text.strip() or not ocr_result.lines:
            self.clear_group(cluster_id)
            return

        translated_lines = [l for l in translated_text.splitlines() if l.strip()]
        ocr_lines: list[LineRect] = ocr_result.lines
        M = len(translated_lines)
        N = len(ocr_lines)

        if M == N:
            # ── ベストケース: 1対1対応 ──────────────────────────────────────
            self._place_lines(cluster_id, translated_lines, ocr_lines)

        elif 1 < M < N:
            # ── 比例分配: LLM が N 行を M 行に圧縮した場合 ─────────────────
            logger.debug(
                "行数不一致 (翻訳 %d 行 vs OCR %d 行)。比例分配で配置。", M, N,
            )
            matched_rects = [
                ocr_lines[round(i * (N - 1) / (M - 1))]
                for i in range(M)
            ]
            self._place_lines(cluster_id, translated_lines, matched_rects)

        else:
            # ── アンカー配置: M==1 または M>N ──────────────────────────────
            logger.debug(
                "行数不一致 (翻訳 %d 行 vs OCR %d 行)。BoundingBox アンカーで配置。",
                M, N,
            )
            anchor_x, anchor_bot, group_w = self._compute_anchor_rect(ocr_lines)
            anchor_lr = LineRect(
                text="",
                local_x=0, local_y=0,
                local_w=max(group_w, 1), local_h=0,
                screen_x=anchor_x,
                screen_y=anchor_bot,
            )
            self._place_lines(cluster_id, [translated_text.strip()], [anchor_lr])

        if self._duration_ms > 0:
            self._expire_timer.start(self._duration_ms)
        else:
            self._expire_timer.stop()

    def clear_group(self, cluster_id: int) -> None:
        """
        指定クラスターの字幕を消去する（パブリック API）。

        世代番号も削除することで、飛行中（in-flight）の翻訳リクエストが
        後から translation_ready を発火させても無視されるようになる。
        """
        if cluster_id in self._groups:
            self._groups.pop(cluster_id).dispose()
            logger.debug("clear_group: cluster=%d のラベルを破棄しました。", cluster_id)
        # 世代番号を削除 = このクラスターの全翻訳は「古い」と宣言
        self._generation.pop(cluster_id, None)
        self._sync_mask_regions()

    def clear_all(self) -> None:
        """全クラスターの字幕を一括消去する（タイマー満了・ウィンドウ消失時）"""
        self._expire_timer.stop()
        for group in self._groups.values():
            group.dispose()
        self._groups.clear()
        self._generation.clear()
        self._sync_mask_regions()

    # ── 内部ヘルパー ─────────────────────────────────────────────────────────

    def _place_lines(
        self, cluster_id: int, texts: list[str], line_rects: list[LineRect]
    ) -> None:
        """
        指定クラスターの字幕ラベルを配置する。

        旧グループがある場合は dispose() で確実に破棄してから
        新しい _SubtitleGroup を生成する。
        プールを使わないため「古い行数のラベルが残る」ミルフィーユが起きない。
        """
        # 旧グループを破棄（deleteLater() で Qt が安全に解放）
        if cluster_id in self._groups:
            self._groups.pop(cluster_id).dispose()

        group = SubtitleManager._SubtitleGroup()
        self._groups[cluster_id] = group

        # ── DPI スケーリング補正 ─────────────────────────────────────────────
        dpr: float = self._parent.devicePixelRatioF()
        parent_geom = self._parent.geometry()
        origin_x: int = parent_geom.x()
        origin_y: int = parent_geom.y()

        for text, lr in zip(texts, line_rects):
            if not text.strip():
                continue
            label = SubtitleLabel(self._cfg, parent=self._parent)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            label.set_text(text)

            # 物理ピクセル座標 → Qt 論理ピクセル座標 → OverlayWindow ローカル座標
            logical_x = round(lr.screen_x / dpr) - origin_x
            logical_y = round((lr.screen_y + lr.local_h + self._offset_y) / dpr) - origin_y

            # 四辺のはみ出し補正
            parent_width  = self._parent.width()
            parent_height = self._parent.height()
            logical_x = max(0, min(logical_x, parent_width  - label.width()))
            logical_y = max(0, min(logical_y, parent_height - label.height()))

            label.move(QPoint(logical_x, logical_y))
            label.show()
            label.raise_()
            group.labels.append(label)
            logger.debug(
                "字幕配置: phys=(%d,%d) dpr=%.2f → logical=(%d,%d) text='%s'",
                lr.screen_x, lr.screen_y + lr.local_h, dpr,
                logical_x, logical_y, text[:20],
            )

        self._sync_mask_regions()

    def _compute_anchor_rect(
        self, lines: list[LineRect]
    ) -> tuple[int, int, int]:
        """
        複数の LineRect から「字幕を配置すべきアンカー座標」を計算する。
        MAD（中央絶対偏差）による外れ値除去 + 残存行の Union BoundingBox。
        """
        if len(lines) == 1:
            lr = lines[0]
            return lr.screen_x, lr.screen_y + lr.local_h, lr.local_w

        center_ys = [lr.screen_y + lr.local_h // 2 for lr in lines]
        sorted_ys = sorted(center_ys)
        n = len(sorted_ys)
        median_y = sorted_ys[n // 2]
        deviations = sorted(abs(cy - median_y) for cy in sorted_ys)
        mad = deviations[n // 2]
        threshold = max(mad * 3.0, 80.0)

        main_lines = [
            lr for lr, cy in zip(lines, center_ys)
            if abs(cy - median_y) <= threshold
        ]
        if not main_lines:
            main_lines = lines

        min_x   = min(lr.screen_x              for lr in main_lines)
        max_x   = max(lr.screen_x + lr.local_w for lr in main_lines)
        max_bot = max(lr.screen_y + lr.local_h  for lr in main_lines)

        logger.debug(
            "_compute_anchor_rect: %d行中%d行を主要グループとして採用 "
            "(median_y=%d, MAD=%d, threshold=%.0f) → anchor=(%d, %d)",
            len(lines), len(main_lines), median_y, mad, threshold,
            min_x, max_bot,
        )

        return min_x, max_bot, max(max_x - min_x, 1)

    def _sync_mask_regions(self) -> None:
        """
        現在表示中の全字幕ラベルの画面絶対座標（物理ピクセル）を
        MaskRegionStore に書き込む（自己マスキングの送信側）。
        """
        if self._mask_store is None:
            return

        dpr: float = self._parent.devicePixelRatioF()
        parent_geom = self._parent.geometry()
        origin_x: int = parent_geom.x()
        origin_y: int = parent_geom.y()

        regions: list[tuple[int, int, int, int]] = []
        for group in self._groups.values():
            for label in group.labels:
                if not label.isVisible():
                    continue
                phys_x = round((origin_x + label.x()) * dpr)
                phys_y = round((origin_y + label.y()) * dpr)
                phys_w = round(label.width()  * dpr)
                phys_h = round(label.height() * dpr)
                regions.append((phys_x, phys_y, phys_w, phys_h))

        self._mask_store.update(regions)


# ══════════════════════════════════════════════════════════════════════════════
#  Part 3  ―  StatusBar ／ MainController
# ══════════════════════════════════════════════════════════════════════════════

class StatusBar(QLabel):
    """
    画面左下に表示するデバッグ用ステータスラベル。
    verbose_logging == True のときのみ有効。エラーは常時表示。
    """

    _AUTO_CLEAR_MS: int = 5_000

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFont(QFont("Consolas", 9))
        self.setStyleSheet(
            "QLabel { color: #00FF88; background-color: rgba(0,0,0,160);"
            " padding: 4px 8px; border-radius: 3px; }"
        )
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._clear_timer = QTimer(self)
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(self.hide)

        self._position_to_bottom_left()
        self.hide()

    def show_message(self, text: str, is_error: bool = False) -> None:
        color = "#FF4444" if is_error else "#00FF88"
        self.setStyleSheet(
            f"QLabel {{ color: {color}; background-color: rgba(0,0,0,160);"
            f" padding: 4px 8px; border-radius: 3px; }}"
        )
        self.setText(text)
        self.adjustSize()
        self._position_to_bottom_left()
        self.show()
        self.raise_()
        self._clear_timer.start(self._AUTO_CLEAR_MS)

    def _position_to_bottom_left(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        margin = 12
        self.move(margin, parent.height() - self.height() - margin)


class MainController(QObject):
    """
    OCR → 翻訳 → 字幕描画 のパイプラインを統括するコントローラー。
    ワーカーの生成・シグナル配線・安全なシャットダウンを担う。

    【v2 変更点】
    - _on_text_detected: 空テキスト時の消去責務をここに集約。
      「空なら clear_group、テキストがあれば世代を付与して翻訳リクエスト」
      という明確な分岐にした。
    - SubtitleManager の内部メソッドへの直接アクセスを廃止。
      clear_group() / stamp_generation() というパブリック API のみを使う。
    """

    def __init__(
        self,
        config: AppConfig,
        overlay: OverlayWindow,
        subtitle_mgr: SubtitleManager,
        mask_store: Optional[MaskRegionStore] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._overlay = overlay
        self._subtitle_mgr = subtitle_mgr
        self._verbose = config.debug.verbose_logging

        self._status_bar = StatusBar(parent=overlay)
        if self._verbose:
            logger.info("verbose_logging が有効です。StatusBar を表示します。")

        self._ocr_worker = OcrWorker(config, mask_store=mask_store)
        self._translator = TranslatorWorker(config)
        self._connect_signals()

        QApplication.instance().aboutToQuit.connect(self._on_quit)  # type: ignore[union-attr]
        logger.info("MainController 初期化完了。")

    def start(self) -> None:
        """両ワーカースレッドを起動する。OverlayWindow.show() の後に呼ぶこと。"""
        logger.info("ワーカースレッド起動中…")
        self._translator.start()
        self._ocr_worker.start()
        logger.info("OCR・翻訳ワーカー起動完了。パイプラインを開始します。")

    def _connect_signals(self) -> None:
        self._ocr_worker.text_detected.connect(self._on_text_detected)
        self._ocr_worker.window_not_found.connect(self._on_window_not_found)
        self._ocr_worker.status_changed.connect(self._on_ocr_status)
        self._ocr_worker.error_occurred.connect(self._on_backend_error)
        self._translator.translation_ready.connect(self._on_translation_ready)
        self._translator.error_occurred.connect(self._on_backend_error)
        logger.debug("シグナル配線完了。")

    @pyqtSlot(OcrResult)
    def _on_text_detected(self, ocr_result: OcrResult) -> None:
        if self._verbose:
            logger.debug(
                "OCR 検知: cluster=%d %r",
                ocr_result.cluster_id, ocr_result.full_text[:60],
            )

        # ── 空の OcrResult: クラスター消失通知 ──────────────────────────────
        # OCR エンジンが cluster_disappear_frames フレーム連続で検出できなかった
        # クラスターについて、空テキストの OcrResult を送出する。
        # clear_group() は世代番号も削除するため、飛行中の翻訳リクエストが
        # 後から translation_ready を発火させてもゾンビ復活は起きない。
        if not ocr_result.full_text.strip():
            self._subtitle_mgr.clear_group(ocr_result.cluster_id)
            return

        # ── 対象ウィンドウのモニターにオーバーレイを追従させる ──────────────
        self._overlay.relocate_to_screen(
            ocr_result.capture_left, ocr_result.capture_top
        )

        # ── 世代番号を付与してから翻訳リクエストを送出 ──────────────────────
        # stamp_generation() は ocr_result.generation を更新して返す。
        # TranslatorWorker はこの OcrResult をそのまま translation_ready で
        # 折り返すため、update_subtitles() で世代照合が可能になる。
        stamped = self._subtitle_mgr.stamp_generation(ocr_result)
        self._translator.request_translation(stamped)

    @pyqtSlot()
    def _on_window_not_found(self) -> None:
        self._subtitle_mgr.clear_all()
        msg = (
            f"ウィンドウ '{self._config.target_window.window_title_keyword}' "
            "が見つかりません。"
        )
        logger.debug(msg)
        if self._verbose:
            self._status_bar.show_message(f"⚠ {msg}")

    @pyqtSlot(str)
    def _on_ocr_status(self, message: str) -> None:
        logger.info("[OCR] %s", message)
        if self._verbose:
            self._status_bar.show_message(f"OCR: {message}")

    @pyqtSlot(str, object)
    def _on_translation_ready(self, translated_text: str, ocr_result: OcrResult) -> None:
        if self._verbose:
            logger.debug(
                "翻訳完了: %r → %r",
                ocr_result.full_text[:40],
                translated_text[:40],
            )
        self._subtitle_mgr.update_subtitles(translated_text, ocr_result)

    @pyqtSlot(str)
    def _on_backend_error(self, message: str) -> None:
        logger.error("[Backend Error] %s", message)
        self._subtitle_mgr.clear_all()
        self._status_bar.show_message(f"✕ {message}", is_error=True)

    @pyqtSlot()
    def _on_quit(self) -> None:
        logger.info("シャットダウン処理を開始します…")

        logger.info("  OcrWorker を停止中…")
        self._ocr_worker.stop()
        if not self._ocr_worker.wait(5_000):
            logger.warning("  OcrWorker が 5 秒以内に終了しませんでした（強制継続）。")
        else:
            logger.info("  OcrWorker 停止完了。")

        logger.info("  TranslatorWorker を停止中…")
        self._translator.stop()
        if not self._translator.wait(8_000):
            logger.warning("  TranslatorWorker が 8 秒以内に終了しませんでした（強制継続）。")
        else:
            logger.info("  TranslatorWorker 停止完了。")

        logger.info("シャットダウン処理が完了しました。")

    # ── 公開API: タスクトレイから呼ぶ ───────────────────────────────────────
    #
    # SystemTrayIcon はスレッドの存在を一切意識しない。
    # 「一時停止」「再開」「キャッシュクリア」「設定変更」の実体は
    # すべてここに閉じ込め、トレイ側は対応するメソッドを呼ぶだけにする。

    @property
    def is_paused(self) -> bool:
        return self._ocr_worker.is_paused

    def pause(self) -> None:
        """
        OCR ワーカーを安全にサスペンドし、同時に現在表示中の字幕を消去する。

        OcrWorker.pause() は asyncio.Event を call_soon_threadsafe 経由で
        clear() するため、進行中のキャプチャ/OCR処理を中断することはない
        （次サイクル開始前で安全に止まる）。
        字幕を残したままにすると、ムービー中などポーズした場面に直前の
        翻訳が表示され続けて配信上不自然になるため、ここで明示的に消す。
        """
        self._ocr_worker.pause()
        self._subtitle_mgr.clear_all()
        logger.info("MainController: パイプラインを一時停止しました（字幕クリア済み）。")

    def resume(self) -> None:
        """一時停止を解除する。次の OCR サイクルから通常通り字幕が再開される。"""
        self._ocr_worker.resume()
        logger.info("MainController: パイプラインを再開しました。")

    def clear_translation_cache(self) -> None:
        """
        翻訳キャッシュをクリアする（Ollama がおかしな訳を出した場合の応急処置）。

        TranslatorWorker.clear_cache() がワーカー自身のスレッド内で
        安全にキャッシュを再生成するため、ここでは単に委譲するだけでよい。
        """
        self._translator.clear_cache()
        logger.info("MainController: 翻訳キャッシュのクリアをリクエストしました。")

    def apply_settings(self, new_config: AppConfig) -> None:
        """
        実行中に SettingsDialog で変更された設定を反映する。

        現状 SettingsDialog が変更を許可するのは target_window のみだが、
        将来的にセクションが増えても安全なように AppConfig を丸ごと
        受け取って自身の _config を差し替える設計にしてある。
        OcrWorker は実行中スレッドであり config をホットスワップできない
        フィールド（capture_interval_ms 等）も持つため、ここでは
        ウィンドウ検索キーワードのみを安全に反映できる範囲で更新する。
        """
        self._config.target_window.window_title_keyword = (
            new_config.target_window.window_title_keyword
        )
        self._ocr_worker.config.target_window.window_title_keyword = (
            new_config.target_window.window_title_keyword
        )
        # キーワードが変わった以上、古いウィンドウ矩形キャッシュは無効。
        # 次の _maybe_refresh_window_rect() で即座に再検索させる。
        self._ocr_worker._window_rect = None
        self._ocr_worker._last_window_search = 0.0
        logger.info(
            "MainController: 設定を反映しました（ターゲットウィンドウ='%s'）。",
            new_config.target_window.window_title_keyword,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Part 4  ―  SettingsDialog ／ SystemTrayIcon
# ══════════════════════════════════════════════════════════════════════════════

class SettingsDialog(QDialog):
    """
    起動時ランチャー兼、実行中の再設定用ダイアログ。

    非エンジニアの友人が config.json を直接編集しなくて済むよう、
    入力項目は「ターゲットウィンドウ名」のみに絞っている。

    【起動時の挙動】
    main() からは config の状態に関わらず必ず一度表示される。
    「保存して起動」を押すと結果を config.json に書き戻してから
    メイン処理（OverlayWindow 等の構築）へ進む。
    「キャンセル」または「×」で閉じた場合、呼び出し側 main() は
    exec() の戻り値（QDialog.DialogCode.Rejected）を見てアプリ自体を
    終了させる。
    """

    def __init__(self, config: AppConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("リアルタイム翻訳オーバーレイ ― 起動設定")
        self.setModal(True)
        self.setMinimumWidth(420)

        self._config = config

        layout = QFormLayout(self)

        intro = QLabel(
            "翻訳したいゲームが表示されているウィンドウのタイトルに\n"
            "含まれるキーワードを入力してください（部分一致でOKです）。\n"
            "例: 'YouTube', 'Discord', 'Steam' など"
        )
        intro.setWordWrap(True)
        layout.addRow(intro)

        self._window_keyword_edit = QLineEdit(
            config.target_window.window_title_keyword
        )
        self._window_keyword_edit.setPlaceholderText("例: YouTube")
        layout.addRow("ターゲットウィンドウ名:", self._window_keyword_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存して起動")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _on_save(self) -> None:
        keyword = self._window_keyword_edit.text().strip()
        if not keyword:
            QMessageBox.warning(
                self, "入力エラー",
                "ターゲットウィンドウ名を入力してください。",
            )
            return

        self._config.target_window.window_title_keyword = keyword

        # ── config.json への安全な書き戻し ──────────────────────────────
        # save_partial_config は対象セクションのキーだけを差し替えるため、
        # "_comment" 系の説明キーや他セクションの値は一切失われない。
        saved = save_partial_config({
            "target_window": {"window_title_keyword": keyword},
        })
        if not saved:
            # 保存に失敗してもアプリの続行は妨げない
            # （今回起動分の設定としてはメモリ上の値がそのまま使われる）。
            QMessageBox.warning(
                self, "保存エラー",
                "config.json への書き込みに失敗しました。\n"
                "今回の起動時のみ、入力した設定で動作します。",
            )
            logger.warning("SettingsDialog: config.json の保存に失敗しました。")

        self.accept()

    def updated_config(self) -> AppConfig:
        """保存後の最新 AppConfig を返す（呼び出し側はこれを使って続行する）"""
        return self._config


class SystemTrayIcon(QSystemTrayIcon):
    """
    タスクトレイ常駐アイコン。ゲーム画面の邪魔にならないよう常駐し、
    右クリックメニューから配信中の柔軟な操作を可能にする。

    【設計方針】
    このクラスはスレッドの存在を一切意識しない。
    [一時停止/再開][翻訳キャッシュをクリア] はいずれも MainController の
    パブリックメソッド（pause/resume/clear_translation_cache）を呼ぶだけで、
    asyncio.Event の call_soon_threadsafe 操作やキャッシュの排他制御は
    すべて MainController 側（さらにその先の OcrWorker/TranslatorWorker）
    に閉じ込められている。
    """

    def __init__(
        self,
        config: AppConfig,
        controller: "MainController",
        app: QApplication,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._controller = controller
        self._app = app

        self.setIcon(self._make_icon())
        self.setToolTip("リアルタイム翻訳オーバーレイ")

        self._menu = QMenu()

        self._settings_action = QAction("設定", self._menu)
        self._settings_action.triggered.connect(self._on_open_settings)
        self._menu.addAction(self._settings_action)

        self._pause_action = QAction("一時停止", self._menu)
        self._pause_action.triggered.connect(self._on_toggle_pause)
        self._menu.addAction(self._pause_action)

        self._clear_cache_action = QAction("翻訳キャッシュをクリア", self._menu)
        self._clear_cache_action.triggered.connect(self._on_clear_cache)
        self._menu.addAction(self._clear_cache_action)

        self._menu.addSeparator()

        self._quit_action = QAction("終了", self._menu)
        self._quit_action.triggered.connect(self._on_quit)
        self._menu.addAction(self._quit_action)

        self.setContextMenu(self._menu)
        self.show()

    # ── アイコン生成（PyInstaller 配布を見据え、外部ファイルに依存しない） ──

    @staticmethod
    def _make_icon() -> QIcon:
        """
        プログラム的に簡易アイコンを生成する。

        ファイルベースのアイコン（.ico 等）に依存すると、PyInstaller での
        パッケージング時にリソースパスの解決（sys._MEIPASS 等）を別途
        ケアする必要が生じる。シンプルな図形を QPainter で描画すれば
        その手間が一切不要になる。
        """
        size = 32
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#3478F6"))
        painter.drawEllipse(2, 2, size - 4, size - 4)

        painter.setPen(QPen(QColor("#FFFFFF"), 2))
        font = QFont("Yu Gothic UI", 14, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(
            pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "訳"
        )
        painter.end()

        return QIcon(pixmap)

    # ── メニューハンドラ ──────────────────────────────────────────────────

    def _on_open_settings(self) -> None:
        dialog = SettingsDialog(self._config)
        result = dialog.exec()
        if result == QDialog.DialogCode.Accepted:
            self._controller.apply_settings(dialog.updated_config())

    def _on_toggle_pause(self) -> None:
        if self._controller.is_paused:
            self._controller.resume()
            self._pause_action.setText("一時停止")
        else:
            self._controller.pause()
            self._pause_action.setText("再開")

    def _on_clear_cache(self) -> None:
        self._controller.clear_translation_cache()
        self.showMessage(
            "翻訳キャッシュをクリアしました",
            "次回以降のテキストは再翻訳されます。",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def _on_quit(self) -> None:
        logger.info("SystemTrayIcon: 終了がリクエストされました。")
        self._app.quit()


# ══════════════════════════════════════════════════════════════════════════════
#  Part 5  ―  ロギング ／ 例外フック ／ main()
# ══════════════════════════════════════════════════════════════════════════════

def _setup_logging(verbose: bool = False) -> None:
    """コンソール＋RotatingFile ハンドラを設定する"""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)-8s] %(name)-14s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # --noconsole (PyInstaller) ビルド時は sys.stderr が None になるため、
    # StreamHandler の追加をスキップする。コンソールがなくても
    # RotatingFileHandler だけで全ログが記録されるため問題ない。
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(fmt)
        root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=_LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("PyQt6").setLevel(logging.WARNING)

    logger.info(
        "ロギング設定完了（レベル: %s / ログファイル: %s）",
        logging.getLevelName(level), _LOG_FILE,
    )


def _install_excepthook() -> None:
    """Python の未捕捉例外をロガーに記録し、QApplication 経由で安全終了する"""

    def _hook(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.critical("未捕捉の例外が発生しました。アプリケーションを終了します。\n%s", tb_str)
        app = QApplication.instance()
        if app is not None:
            app.quit()
        else:
            sys.exit(1)

    sys.excepthook = _hook
    logger.debug("sys.excepthook を設定しました。")


def main() -> None:
    """
    起動シーケンス:
        ① 設定ロード  ② ロギング  ③ 例外フック  ④ QApplication
        ⑤ SettingsDialog（必須・キャンセルで終了）
        ⑥ OverlayWindow  ⑦ SubtitleManager  ⑧ MainController
        ⑨ SystemTrayIcon  ⑩ ウィンドウ表示（Win32 フラグ確定）
        ⑪ パイプライン起動  ⑫ イベントループ  ⑬ 後処理
    """
    config = load_config()                              # ①
    _setup_logging(verbose=config.debug.verbose_logging) # ②
    logger.info("═" * 60)
    logger.info("リアルタイム翻訳オーバーレイ 起動中…")
    logger.info("  対象ウィンドウ       : %s", config.target_window.window_title_keyword)
    logger.info("  翻訳モデル           : %s", config.translation.model_name)
    logger.info("  Ollama エンドポイント: %s", config.translation.ollama_url)
    logger.info("═" * 60)

    _install_excepthook()                               # ③

    QApplication.setHighDpiScaleFactorRoundingPolicy(   # ④
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # ── ⑤ 起動時ランチャーGUI（必須表示） ─────────────────────────────────
    # config の状態に関わらず、毎回必ず SettingsDialog を表示する。
    # 「保存して起動」(Accepted) のみメイン処理に進み、
    # 「キャンセル」「×ボタン」(Rejected) はアプリ自体を終了する。
    launcher = SettingsDialog(config)
    if launcher.exec() != QDialog.DialogCode.Accepted:
        logger.info("起動設定がキャンセルされました。アプリケーションを終了します。")
        sys.exit(0)
    config = launcher.updated_config()
    logger.info(
        "起動設定を確認しました（ターゲットウィンドウ='%s'）。",
        config.target_window.window_title_keyword,
    )

    overlay = OverlayWindow(config)                     # ⑥

    mask_store = MaskRegionStore()

    subtitle_mgr = SubtitleManager(                     # ⑦
        parent=overlay,
        cfg=config.overlay,
        mask_store=mask_store,
    )
    controller = MainController(                        # ⑧
        config=config,
        overlay=overlay,
        subtitle_mgr=subtitle_mgr,
        mask_store=mask_store,
        parent=app,
    )

    tray_icon = SystemTrayIcon(                          # ⑨
        config=config, controller=controller, app=app, parent=app,
    )
    logger.info("SystemTrayIcon を表示しました。")

    overlay.showFullScreen()                            # ⑩
    logger.info("OverlayWindow を表示しました（フルスクリーン透過モード）。")

    controller.start()                                  # ⑪
    logger.info("パイプライン起動完了。イベントループに入ります。")

    exit_code = app.exec()                              # ⑫

    logger.info("アプリケーションが終了しました（終了コード: %d）。", exit_code) # ⑬
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

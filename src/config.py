"""
config.py - 設定ローダー & デフォルト値の定義

役割:
  - config.json を読み込み、各セクションを型付きデータクラスとして提供する
  - config.json が存在しない、または一部キーが欠けている場合は DEFAULT_CONFIG で補完する
  - 他のモジュールは `from config import load_config` で設定を取得する
  - SettingsDialog からの書き戻しは `save_partial_config` を使う。
    既存の config.json をパースした dict に対し、指定したキーパスだけを
    上書きしてから書き戻すため、"_comment" 系の説明キーやユーザーが
    手動編集した未知のキーが失われない。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  ユーザーデータのベースディレクトリ（exe の隣）
# ─────────────────────────────────────────────
#
#  【PyInstaller の --onefile モードにおけるパス問題】
#
#  PyInstaller で --onefile ビルドした場合、起動時に
#  sys._MEIPASS（例: C:\Users\<User>\AppData\Local\Temp\_MEIxxxxx）
#  という読み取り専用の一時フォルダへ内部モジュールが展開される。
#  この状態では __file__ は _MEIPASS 内部のパスに解決されるため、
#  「__file__.parent に config.json を置く」方式では
#  一時フォルダに書き込もうとして失敗したり、
#  ユーザーがアクセスできない場所にファイルが生成される。
#
#  sys._MEIPASS  : バンドル内リソースの読み取り専用展開先（今回は不要）
#  sys.executable: 実際の .exe ファイル自体のフルパス
#                  → .parent が「.exe の隣のフォルダ」
#                  → ユーザーが読み書きする config.json / overlay.log はここに置く
#
#  通常の Python 実行時（開発・テスト）は sys.frozen が存在しないため、
#  従来通り __file__.parent（スクリプトと同じディレクトリ）を使う。
#
def _get_app_dir() -> Path:
    """
    config.json / overlay.log などユーザーデータを置くディレクトリを返す。

    - exe 実行時 (PyInstaller --onefile): sys.executable の親ディレクトリ
      （= デスクトップ等、ユーザーが exe を置いた場所）
    - 通常の Python 実行時               : このスクリプトファイルの親ディレクトリ
    """
    import sys
    if getattr(sys, "frozen", False):
        # PyInstaller でビルドされた exe として実行中
        return Path(sys.executable).parent
    else:
        # 通常の `python main.py` または開発環境
        return Path(__file__).parent


APP_DIR     = _get_app_dir()
CONFIG_FILE = APP_DIR / "config.json"


# ─────────────────────────────────────────────
#  セクション別 データクラス
# ─────────────────────────────────────────────

@dataclass
class TargetWindowConfig:
    window_title_keyword: str  = "YouTube"
    window_search_interval_ms: int = 3000


@dataclass
class CropRegion:
    """ウィンドウ相対の比率で指定（0.0〜1.0）"""
    left_ratio:   float = 0.0
    top_ratio:    float = 0.6
    right_ratio:  float = 1.0
    bottom_ratio: float = 1.0


@dataclass
class CaptureConfig:
    crop_region: CropRegion | None = None
    capture_interval_ms: int = 500


@dataclass
class OcrConfig:
    language: str               = "en-US"
    similarity_threshold: float = 0.95

    # ── ヒステリシス（変化確定の状態ロック） ─────────────────────────────────
    # similarity_threshold との組み合わせで3段階判定を構成する。
    #
    #   similarity >= similarity_threshold (0.95)
    #       → 変化なし。スキップ。
    #
    #   similarity < similarity_lower_threshold (0.80)
    #       → 大幅な変化。即座に emit。
    #       （例: 画面テキストが完全に切り替わった、重要な単語が変わった）
    #
    #   similarity_lower_threshold <= similarity < similarity_threshold  ← グレーゾーン
    #       → OCR 読みブレの可能性が高い帯域。
    #         同一テキストが text_confirm_frames フレーム連続したときだけ emit。
    #         これにより "A→B→A→B" の振動ノイズを抑制しつつ、
    #         "Red key → Blue key" のような実質的な1単語変化も
    #         text_confirm_frames フレーム後に確実に反映する。
    #
    # similarity_lower_threshold の推奨値:
    #   0.75: より保守的（グレーゾーンを広く取る）
    #   0.80: 推奨（ログ実測値 0.857 を確実にグレーゾーンに収める）
    #   0.85: 積極的（グレーゾーンを狭く取るが長文で副作用の恐れあり）
    similarity_lower_threshold: float = 0.80

    # text_confirm_frames:
    #   グレーゾーン判定の確定に必要な連続フレーム数。
    #   2 = cluster_confirm_frames と同じ感覚で 1 フレーム猶予（推奨）
    #   3 = より保守的（確定まで最大 1.5 秒）
    text_confirm_frames: int = 2

    # ── 動的クラスタリング (提案3) ────────────────────────────────────────────
    # ギャップ閾値をキャプチャ幅の固定比率ではなく、
    # 「検出行の median(高さ) の N 倍」で計算する。
    # フォントサイズが異なるゲームでも自動スケールする。
    #
    # ── 統計的ギャップ検出 (自然ブレーク検出) ──────────────────────────────────
    # 固定の倍率ではなく「隣接アイテム間ギャップの中央値 × k」を閾値として
    # 動的に算出する。多数の通常ギャップに対して少数の異常に大きいギャップが
    # 存在する、という前提に基づく頑健な統計手法（中央値は外れ値に強い）。
    #
    # cluster_x_gap_k:
    #   行のX方向クラスタリング（_cluster_lines Step2 列分割）に使う倍率。
    #   「中央値ギャップの何倍を超えたら別カラムとみなすか」
    cluster_x_gap_k: float = 3.0
    # cluster_x_gap_min_px:
    #   上記のギャップ閾値の最低保証値（px）。
    #   サンプル数が少なく中央値が統計的に信頼できない場合のフォールバック。
    cluster_x_gap_min_px: float = 40.0

    # word_split_gap_k / word_split_min_gap_px:
    #   _do_ocr 内、WinRT の OcrLine が異なるカラムの単語を強制結合してしまう
    #   問題に対処するための、単語(OcrWord)レベルでの強制X分割に使う閾値。
    #   行間より単語間の方が間隔が小さいため、line用とは別の値を持つ。
    word_split_gap_k: float = 4.0
    word_split_min_gap_px: float = 20.0

    # cluster_y_scale:
    #   Y方向のギャップ判定倍率。行高さの何倍以上の垂直距離で別バンドとみなすか。
    #   例: 行高=30px なら 1.0倍 → 30px 以上で別バンド
    #   小さくすると段落内の行間でも分割が起きるため注意（0.4〜2.0 が実用範囲）
    cluster_y_scale: float = 1.0

    # ── 空間トラッキング（永続クラスターID） ────────────────────────────────
    # フレーム間でクラスターの空間的同一性を判定する IoU (Intersection over
    # Union) の閾値。これを超える重なりがあれば「同じクラスター」とみなし、
    # 前フレームと同じ persistent ID を引き継ぐ。
    cluster_tracking_iou_threshold: float = 0.15

    # ── フレーム間多数決（OCRノイズ吸収） ────────────────────────────────────
    # 同一クラスターの直近 N フレーム分のOCRテキストを集計し、
    # 最も出現回数の多いテキストを「代表テキスト」として採用する。
    # 背景アニメーション等で1〜2フレームだけ誤読が混じっても
    # 翻訳・再描画が発生しなくなる。
    #
    # 1 = 多数決なし（直前1フレームのテキストをそのまま使う / 無効化）
    # 3 = 直近3フレームで多数決（ノイズ率33%未満を吸収、推奨）
    # 5 = 直近5フレームで多数決（ノイズ率40%未満を吸収 / 変化検知が最大2秒遅延）
    majority_vote_frames: int = 3
    # 1 = 初回検出ですぐ確定（背景ノイズが即字幕化しやすい）
    # 2 = 初回検出＋次フレームも連続検出で確定（推奨）
    # 3 = より保守的（3フレーム連続で初めて確定）
    cluster_confirm_frames: int = 2

    # ── 幽霊字幕の消去デバウンス (提案1) ────────────────────────────────────
    # あるクラスターが何フレーム連続で検出されなかった場合に
    # 「消滅確定」と判断して字幕を消去するか。
    # 1 = 1フレームでも消えたら即消去（OCRノイズで誤消去しやすい）
    # 2 = 2フレーム連続欠落で消去（500ms間隔なら1秒の猶予、推奨）
    # 3 = 3フレーム連続で消去（より保守的）
    cluster_disappear_frames: int = 2


@dataclass
class TranslationConfig:
    ollama_url: str          = "http://localhost:11434/api/generate"
    model_name: str          = "qwen2.5:1.5b"
    request_timeout_sec: int = 15
    cache_max_size: int      = 200
    prompt_template: str     = (
        "Translate the following English text to Japanese. "
        "Output only the translated text, no explanations.\n\n{text}"
    )


@dataclass
class OverlayConfig:
    font_family: str          = "Yu Gothic UI"
    font_size_pt: int         = 14
    font_color: str           = "#FFFFFF"
    background_color: str     = "#CC000000"
    popup_offset_y_px: int    = 8
    padding_x_px: int         = 10
    padding_y_px: int         = 6
    display_duration_sec: int = 0
    always_on_top: bool       = True


@dataclass
class DebugConfig:
    verbose_logging: bool = False
    save_captures: bool   = False


# ─────────────────────────────────────────────
#  トップレベル設定クラス
# ─────────────────────────────────────────────

@dataclass
class AppConfig:
    target_window: TargetWindowConfig = field(default_factory=TargetWindowConfig)
    capture: CaptureConfig            = field(default_factory=CaptureConfig)
    ocr: OcrConfig                    = field(default_factory=OcrConfig)
    translation: TranslationConfig    = field(default_factory=TranslationConfig)
    overlay: OverlayConfig            = field(default_factory=OverlayConfig)
    debug: DebugConfig                = field(default_factory=DebugConfig)


# ─────────────────────────────────────────────
#  内部ヘルパー: dict → データクラスへの安全な変換
# ─────────────────────────────────────────────

def _merge(dataclass_instance, source_dict: dict):
    """
    source_dict に存在するキーだけを dataclass_instance に上書きする。
    JSON にないキーはデータクラスのデフォルト値が使われる（壊れにくい設計）。
    """
    for key, value in source_dict.items():
        if key.startswith("_comment"):
            continue  # JSONの説明コメントキーは無視
        if hasattr(dataclass_instance, key):
            current = getattr(dataclass_instance, key)
            # ネストされたデータクラスの場合は再帰的にマージ
            if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
                _merge(current, value)
            else:
                setattr(dataclass_instance, key, value)
        else:
            logger.warning("config.json に未知のキーがあります（無視します）: '%s'", key)


def _parse_crop_region(raw: dict | None) -> CropRegion | None:
    """crop_region が null の場合は None を返す（ウィンドウ全体をキャプチャ）"""
    if raw is None:
        return None
    region = CropRegion()
    _merge(region, raw)
    return region


# ─────────────────────────────────────────────
#  公開API: load_config()
# ─────────────────────────────────────────────

def load_config(path: Path = CONFIG_FILE) -> AppConfig:
    """
    config.json を読み込んで AppConfig を返す。

    - ファイルが存在しない場合 → 全デフォルト値で動作（警告ログのみ）
    - JSONパースエラーの場合   → 全デフォルト値で動作（エラーログ）
    - 一部キーが欠けている場合 → 欠けている部分だけデフォルト値で補完
    """
    config = AppConfig()

    if not path.exists():
        logger.warning(
            "config.json が見つかりません（%s）。デフォルト設定で起動します。", path
        )
        return config

    try:
        with path.open(encoding="utf-8") as f:
            raw: dict = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("config.json のパースに失敗しました: %s。デフォルト設定で起動します。", e)
        return config

    # --- 各セクションをマージ ---
    if section := raw.get("target_window"):
        _merge(config.target_window, section)

    if section := raw.get("capture"):
        # crop_region だけ特別処理（null を許容するため）
        config.capture.crop_region = _parse_crop_region(section.get("crop_region", {}))
        section_without_crop = {k: v for k, v in section.items() if k != "crop_region"}
        _merge(config.capture, section_without_crop)

    if section := raw.get("ocr"):
        _merge(config.ocr, section)

    if section := raw.get("translation"):
        _merge(config.translation, section)

    if section := raw.get("overlay"):
        _merge(config.overlay, section)

    if section := raw.get("debug"):
        _merge(config.debug, section)

    logger.info("設定を読み込みました: %s", path)
    return config


# ─────────────────────────────────────────────
#  公開API: save_partial_config()
# ─────────────────────────────────────────────

def save_partial_config(
    updates: dict[str, dict],
    path: Path = CONFIG_FILE,
) -> bool:
    """
    config.json の一部セクションのキーだけを安全に上書き保存する。

    SettingsDialog のような「一部の値だけをユーザーに変更させる」GUI からの
    書き戻し用。AppConfig を丸ごと dataclasses.asdict() してダンプする方式
    だと "_comment" 系の説明キーがすべて失われてしまうため、代わりに
    既存ファイルを生の dict として読み込み、updates で指定されたキーパス
    だけを書き換えてから保存する。

    Args:
        updates: { セクション名: { キー名: 新しい値, ... }, ... }
                 例: {"target_window": {"window_title_keyword": "Dolphin"}}
        path:    書き込み先（デフォルトは config.json）

    Returns:
        保存に成功した場合 True。失敗した場合は False（ログにエラーを出力）。

    注意:
        - 指定したセクション・キーが既存ファイルに存在しない場合は新規追加する。
        - "_comment" で始まるキーは updates 経由でも上書き対象にしないこと
          （呼び出し側の責務。本関数は単純なキー差し替えのみ行う）。
        - ファイルが存在しない、または壊れている場合は新規に作成する
          （その場合 _comment は当然含まれない）。
    """
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                raw: dict = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(
                "save_partial_config: 既存 config.json のパースに失敗しました "
                "(%s)。空の設定として新規作成します。", e,
            )
            raw = asdict(AppConfig())
    else:
        # 初回起動等でファイルがまだ存在しない場合、AppConfig() の全デフォルト値を
        # ベースとして書き込む。これにより、ここで指定したセクション以外
        # （ocr / translation / overlay / debug 等）も欠落なく保存され、
        # 次回 load_config() 時に「開発環境にしかないモデル名」等の
        # 意図しないデフォルト値で補完されるのを防ぐ。
        raw = asdict(AppConfig())

    for section_name, section_updates in updates.items():
        section = raw.setdefault(section_name, {})
        if not isinstance(section, dict):
            logger.warning(
                "save_partial_config: セクション '%s' が dict ではありません。"
                "上書きします。", section_name,
            )
            section = {}
            raw[section_name] = section
        section.update(section_updates)

    try:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        tmp_path.replace(path)  # 書き込み完了後にアトミックに置き換える
    except OSError as e:
        logger.error("save_partial_config: config.json の書き込みに失敗しました: %s", e)
        return False

    logger.info("設定を保存しました: %s（更新セクション: %s）", path, list(updates.keys()))
    return True

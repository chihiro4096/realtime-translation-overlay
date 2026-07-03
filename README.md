# Realtime Translation Overlay

ローカルLLM（Ollama）とWindows標準の OCR エンジン（winsdk）を利用して、指定したウィンドウ内のテキストをリアルタイムに翻訳し、透過・クリックスルーのオーバーレイウィンドウ上に表示するツールです。

## 1. 特徴

- **完全ローカル動作** — 外部APIを使用しないため、無料かつセキュアに利用できます
- **軽量なOCR** — Windows標準のOCRエンジン（winsdk）を使用するため、追加の重いOCRライブラリが不要です
- **ゲームの邪魔をしない表示** — 透過＆クリックスルーのオーバーレイウィンドウなので、プレイ操作を妨げません

## 2. 必須の準備（Ollamaの導入）

本ツールを動作させるには、以下の2つの準備が必要です。

### ① Windowsの言語パック（英語）の追加

Windows標準のOCRエンジンを使用するため、読み取りたい言語（英語）のパックがOSにインストールされている必要があります。

1. Windowsの設定 > 「時刻と言語」 > 「言語と地域」を開きます。
2. 「言語の追加」から **English (United States)** を検索し、インストールしてください。（※表示言語を変更する必要はありません）

### ② Ollamaの導入とモデルのダウンロード

翻訳にはローカルLLMランタイム「Ollama」が必要です。以下の手順で事前に準備してください。

1. [Ollama公式サイト](https://ollama.com/download) にアクセスし、Windows版のインストーラーをダウンロードして実行します。
2. インストール完了後、コマンドプロンプトまたはPowerShellを開き、以下のコマンドを実行して翻訳用モデルをダウンロードします。

```powershell
ollama pull qwen2.5:3b
```

3. ダウンロードが完了すれば準備は完了です。以下のコマンドでモデルが一覧に表示されることを確認できます。

```powershell
ollama list
```

## 3. 使い方

### A. exeファイルを利用する場合（非エンジニア向け）

1. このリポジトリの [Releases](../../releases) ページから `overlay.exe` をダウンロードします。
2. `overlay.exe` をダブルクリックして起動します。
3. 起動時に設定画面が表示されるので、対象のウィンドウ名（例: `Steam`, `YouTube` など、翻訳したいウィンドウのタイトルに含まれるキーワード）を入力し、「保存して起動」を押します。
4. `overlay.exe` と同じフォルダに、設定ファイル `config.json` とログファイル `overlay.log` が自動生成されます。
5. タスクトレイのアイコンを右クリックすると、以下の操作ができます。
   - **設定**: ウィンドウ名の再設定
   - **一時停止 / 再開**: 翻訳処理の一時停止・再開
   - **翻訳キャッシュをクリア**: キャッシュされた翻訳結果の削除
   - **終了**: アプリケーションの終了

### B. Python環境から実行する場合（開発者向け）

1. [Python公式サイト](https://www.python.org/downloads/) からPythonをインストールします。インストーラー実行時に **「Add python.exe to PATH」に必ずチェックを入れてください。**
2. リポジトリをクローンします。

```powershell
git clone https://github.com/oshimachihiro/realtime-translation-overlay.git
```

3. クローンしたディレクトリに移動します。

```powershell
cd realtime-translation-overlay
```

4. 仮想環境を作成します。

```powershell
python -m venv .venv
```

5. 仮想環境を有効化します。

```powershell
.\.venv\Scripts\activate
```

6. 依存パッケージをインストールします。

```powershell
python -m pip install -r requirements.txt
```

7. アプリケーションを起動します。

```powershell
cd src
python main.py
```

#### おまけ：自分でexe化する

```powershell
python -m PyInstaller overlay.spec
```

## 4. トラブルシューティング

### 画面に赤字で `Client error '404 Not Found' for url 'http://localhost:11434/api/generate'` と表示される

`config.json` の `translation.model_name` に指定されているモデルが、Ollamaにダウンロード（pull）されていないことが原因です。以下のコマンドで、現在ダウンロード済みのモデル一覧を確認してください。

```powershell
ollama list
```

一覧にモデルが存在しない場合は、以下のコマンドで改めてダウンロードしてください。

```powershell
ollama pull qwen2.5:3b
```

### 別のモデルを使いたい

`config.json` を開き、`translation` セクション内の `model_name` を、`ollama list` で確認できる任意のモデル名に書き換えてください。

```json
"translation": {
  "model_name": "任意のモデル名"
}
```

## License

[MIT License](LICENSE)

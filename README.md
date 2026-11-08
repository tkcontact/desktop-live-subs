# desktop-live-subs

Windows で流れている英語の音声 (ブラウザ・ゲーム・通話など、既定の出力デバイスの音すべて) を、
リアルタイムで日本語字幕にして画面下に出す常駐ツール。

```
[Whisper 方式]          WASAPI ループバック → Silero VAD で無音区切り → faster-whisper large-v3-turbo (CUDA, int8)
[ライブキャプション方式] Windows のライブキャプション → UI Automation で字幕テキストを読む → 確定した文だけ取り出す
        ↓ 短い断片はまとめる・同じ文は二度訳さない
Strata (Qwen3.8-Flash-Next Gyro-S, OpenAI 互換 API) で日本語訳 (直前 4 文を文脈に) → 最前面・半透明の字幕ウィンドウ
```

字幕ウィンドウとログの形式は [twitch-live-subs](../twitch-live-subs) のものを元にしている。

## 使い方

`start.cmd` をダブルクリックする。Strata を起動してから字幕ツールが立ち上がり、タスクトレイに「字」のアイコンが出る。

| 操作 | 既定のキー |
| --- | --- |
| クリック透過の ON/OFF (OFF のときは青い枠が出て、ドラッグで移動・右下の ◢ でサイズ変更・右クリックでメニュー) | Ctrl+Alt+T |
| 字幕の表示/非表示 | Ctrl+Alt+H |
| 一時停止/再開 | Ctrl+Alt+P |

トレイのメニュー: 一時停止、字幕の表示、クリック透過、原文の表示、音声認識の方式 (Whisper / ライブキャプション)、
プロファイルの切り替え、ログのフォルダ、終了。終了すると Strata も止める (設定で変えられる)。

ゲームの上に重ねるときは、ゲームを「ボーダーレス」か「ウィンドウ」表示にする (排他フルスクリーンだと字幕が隠れる)。

### ゲームプロファイル

`profiles/<名前>.json` を置くとトレイの「プロファイル」に出る (例: [profiles/example.json](profiles/example.json))。

```json
{
  "description": "何の音声か (翻訳プロンプトに入る)",
  "proper_nouns": ["Whisper に聞き取らせたい固有名詞 (initial_prompt に入る)"],
  "glossary": {"英語": "日本語の訳語 (翻訳プロンプトに入る)"}
}
```

ライブキャプション方式では `proper_nouns` は使われない (固有名詞を渡す手段がない)。

### 設定 (settings.json)

トレイや字幕ウィンドウで変えたもの (方式・プロファイル・位置・大きさ・文字サイズなど) は自動で保存される。
手で変えるもの:

| キー | 既定 | 内容 |
| --- | --- | --- |
| `hotkeys` | 上の表 | `"ctrl+alt+t"` の形式。修飾キーは ctrl / alt / shift / win、キーは英数字か f1〜f12 |
| `alpha` | 0.8 | 字幕ウィンドウの不透明度 |
| `clear_after_sec` | 12 | 新しい字幕が来なければ消すまでの秒数 (0 で消さない) |
| `context` | 4 | 文脈として渡す直前の文の数 |
| `stop_strata_on_exit` | true | 終了時に Strata も止めるか |
| `strata_extra_reserve_mib` | 0 | GPU を使うゲームと併用するときに Strata の VRAM 予約へ足す量 |
| `compute_type` / `beam_size` | int8 / 5 | faster-whisper の設定 |
| `min_silence_ms` / `max_chunk_sec` | 500 / 8 | 発話の区切りとみなす無音の長さと、1 回に文字起こしする最大の長さ |

### ログ

`logs/` に、起動ごとの TSV (原文・訳文・各段の所要時間) と `app.log` を残す。

- `src_delay`: 発話が終わってから原文が出るまで、`delay`: 訳文が出るまで (Whisper 方式)
- `asr_sec`: Whisper の処理時間、`llm_ttft` / `llm_sec`: Strata の最初の文字までと応答完了までの時間
- ライブキャプション方式の遅れは、ライブキャプション上で文が確定してから訳文が出るまで (ライブキャプション自体の遅れは含まない)

動作確認用に `run.cmd --file 音声ファイル --debug` で、ループバックの代わりにファイルを実時間で流してコンソールで見られる。

## セットアップ

1. `setup.cmd` を実行 (`%LOCALAPPDATA%\desktop-live-subs\venv` に仮想環境を作る)。
   CUDA Toolkit / cuDNN は入れなくてよい (CTranslate2 の Windows 版に cuDNN が入っていて、cuBLAS は pip の `nvidia-cublas-cu12` を使う)。
2. Strata の Terminal を入れて、モデルを用意する (60GB ほどダウンロードする):

   ```powershell
   irm https://github.com/Niko1221/Strata/releases/latest/download/install.ps1 | iex
   # 新しいターミナルで
   strata launch --family gyro --model Gyro-S --context 8192 --vision off --parallel 1 --save-preset live-subs --yes --no-tui
   ```

3. ライブキャプション方式を使うなら、ライブキャプションに英語の音声パックを入れる (下記)。

### ライブキャプションの準備

1. `Win + Ctrl + L` でライブキャプションを開く (初回は「ライブキャプションを使い始める」の説明が出るので同意する)。
2. ライブキャプションの右上の ⚙ → 「言語を変更」(または「言語」) → 「英語 (米国)」を選ぶ。
   入っていなければ「言語を追加」から英語 (米国) を追加し、音声パックのダウンロードが終わるまで待つ。
3. ⚙ → 「位置」→「画面上にフローティング」にしておく (上に固定だと画面の上端が字幕用に確保されたままになる)。
4. ⚙ → 「マイクの音声を含める」はオフのまま (自分の声まで訳される)。

字幕ツールはライブキャプションを最小化してタスクバーから隠して使い、方式を戻すと閉じる (もともと開いていたなら元に戻す)。
UI Automation で読むのに特別な設定は要らない。

## メモ

- Strata のモデル: Qwen3.8-Flash-Next は 125B の MoE で、推奨の ISTA-DASLab Q2_0 / IQ2_XS でも RAM が 44GB ほど要る。
  RAM 32GB では `strata launch --plan` が「このPCには収まらない」と判定し、収まるのは Gyro-S と Coder だけだった。
  Coder は英語以外 (日本語を含む) が弱いので、Gyro-S (VRAM+RAM 27.6GB、品質は控えめ) にした。
- VRAM の分け方: Strata は起動時の空き VRAM から `--vram-reserve-mib` を引いた分をエキスパートのキャッシュに使う
  (`--expert-cache auto`)。`vram.py` で Whisper を読み込んで数回文字起こしし、その間の VRAM 増加の最大値を測って
  `vram.json` に保存し、予約量 = 1536 (Strata が画面表示用に空ける既定値) + Whisper の最大値 + 256 を `start.cmd` が渡す。
  Whisper の設定を変えると測り直す。
- 思考モード: Qwen3.8-Flash-Next は既定で思考する (high)。`reasoning_effort: "none"` で切っている。
- Whisper 方式の区切り: 無音 500ms で区切った発話ごとに文字起こしし、文末で終わっていない短い断片 (6 語未満) や 4 語未満の文は、
  次の発話を待ってまとめる (発話が止んで 1.2 秒、最長 4 秒で待つのをやめて出す)。
- ライブキャプション方式の確定: 字幕テキストは途中で書き換わり、古い行は上から消えていく。前回のテキストと語単位で突き合わせ
  (difflib) て「どこまで訳したか」を今回のテキスト上の位置に移し替え、その先で確定したものだけを訳す。確定とみなすのは
  文末の記号の後に続きが出たとき (末尾なら 0.4 秒変わらなければ)、改行が入ったとき、1.2 秒変わらないとき (無音)。
- 二重翻訳の防止: どちらの方式でも、直近 30 秒に訳した文と同じかほぼ同じ (類似度 0.9 以上) 文は訳さない。
- Strata が落ちている・応答しないときは字幕ウィンドウの上の行にエラーを出し、2 秒ごとに `/health` を確かめる。
  その間も原文は出し続け、復旧したら最新の 1 文 (20 秒以内のもの) から訳を再開する。
- `start.cmd` は `\\wsl.localhost` のフォルダを作業フォルダにできないので、`%LOCALAPPDATA%\desktop-live-subs` に移ってから、
  スクリプトを UNC パスのまま `pythonw.exe` に渡している (pushd のドライブ文字は popd で消えるため)。
- 字幕ウィンドウ: Tk は表示するまで外側のウィンドウを作らず、それまで `winfo_id` の親がない。クリック透過
  (`WS_EX_TRANSPARENT`) は表示してから `GetAncestor(GA_ROOT)` で取った外側のウィンドウに付ける
  (子ウィンドウに `WS_EX_LAYERED` が付くと何も描画されなくなる)。

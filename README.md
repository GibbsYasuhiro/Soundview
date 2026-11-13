# Soundview

PCで再生中の音に反応して、シリアル接続の3.5インチLCD（Turing Smart Screen "Rev A" 互換、480x320）に表示するビジュアライザーです。

- **スペクトラム**: 32バンドのLEDセグメント風バー（ピークホールド付き）
- **L/Rメーター**: 左右それぞれのアナログ針メーター（-40〜+3 dB、ピーク表示付き）

## 動作環境

- Windows 10/11（WASAPI ループバックで再生音を取得するため、マイクや設定変更は不要）
- Turing Smart Screen Rev A 互換の3.5インチUSB LCD（USB ID `1A86:5722`。COM番号は自動検出）
  - 「AIDA64対応」として売られている3.5インチUSBモニターの多くが該当します。ただし本ツールはAIDA64とは無関係で、単体で動作します。

## 使い方

```
pip install -r requirements.txt
python soundview.py
```

Releases の `Soundview.exe` を使えば Python は不要です。

| オプション | 内容 |
|---|---|
| `--port COM3` | COMポートを指定（省略時は自動検出） |
| `--mode spectrum\|meter` | 開始時の表示（既定: spectrum） |
| `--cycle SEC` | SEC秒ごとに表示を自動で切り替え |
| `--no-rotate` | 画面が正立で取り付けられている場合に指定 |

> 既定では画面が **上下逆さ** に取り付けられている前提（180度回転して送信）です。向きが逆なら `--no-rotate` を付けてください。

コンソール上のキー操作: `m` / Space = 切替、`s` = スペクトラム、`l` = メーター、`q` = 終了。

## exeの作り方

```
pip install pyinstaller
pyinstaller --onefile --console --name Soundview --collect-all pyaudiowpatch soundview.py
```

## 実装メモ

- この画面はUSBシリアルの転送が約160〜200KB/s程度なので、全画面転送は約2fpsです。変化した領域（LEDセグメント単位）だけを送って高速化しています。
- **コマンドヘッダー(6バイト)と画像データは別々の書き込みで送る必要があります。** 1回の書き込みにまとめると表示が崩れます。
- 他のプログラム（例: LCDを使う別ツール）がCOMポートを開いていると同時には使えません。

## ライセンス

MIT

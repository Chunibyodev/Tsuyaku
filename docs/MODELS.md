# Choosing a translation model

The settings page lists the models below, grouped by the GPU they need with the Whisper speech
model on the same card. This page gives the evidence behind the notes in that list.

## Published benchmarks

**JP-TL-Bench** (Shisa.AI, [paper](https://arxiv.org/abs/2601.00223)) compares Japanese↔English
translations pairwise with an LLM judge. Win rates for models in the list or close relatives:

| Model | JA→EN | EN→JA |
|---|---|---|
| Gemini 2.5 Flash (for reference) | 90.4% | 95.4% |
| Ministral 3 14B Instruct 2512 | 90.7% | 78.9% |
| Gemma 3 27B (Gemma 4's predecessor) | 82.7% | 82.2% |
| Shisa v2.1 Qwen3-8B | 73.7% | 75.1% |
| Qwen3-8B | 64.6% | 57.9% |
| Qwen3-4B | 50.4% | 39.3% |

The Qwen3-4B there is the older hybrid model, not the 2507 Instruct that Tsuyaku uses by default;
Hy-MT2 and Gemma 4 aren't in it.

**Hy-MT2 report** (Tencent, [paper](https://arxiv.org/abs/2605.22064)): XCOMET-XXL on FLORES-200
English↔X, models in non-thinking mode: Hy-MT2-30B-A3B 93.85, Hy-MT2-7B 93.52, Gemma 4 26B-A4B
92.80, Qwen3.6-35B-A3B 90.87, Hy-MT2-1.8B 90.00, Gemma 4 E4B 89.54.

## Side-by-side test

The same 10 lines of a (made-up) Minecraft stream, 10 chat messages and 4 messages to send (in
casual and polite tone) went through Tsuyaku's own translators, with the default glossary plus
ぺこら = Pekora. llama.cpp ran on a CPU, so the timings say nothing about GPU speed; on a GPU, a line
takes roughly in proportion to the model file's size.

| Japanese | Qwen3-4B 2507 (default) | Gemma 4 E4B | Hy-MT2-7B | Shisa v2.1 Qwen3-8B |
|---|---|---|---|---|
| えーみんなこんばんは、今日はね、マイクラやっていこうと思います | Uh, everyone, good night! Today, I think we'll play Minecraft. | Uh, everyone, good evening. Today, I think I'll play Minecraft. | Uh, hello everyone! Tonight, I plan to play Minecraft. | Hey everyone, good evening! Today, I'm gonna be playing Minecraft! |
| ちょっとだけ一人で掘ってたんだけど | I just dug a bit alone. | I was digging by myself for a little bit. | I was digging by myself for a bit. | I just snuck off for a little bit of solo mining. |
| え、待って待って、クリーパーいるじゃん | Wait, wait, isn't there a creeper? | Wait, wait, there's a Creeper! | Huh, wait, wait—there are creepers here! | Wait, wait, there's a creeper! |
| やばいやばい逃げろ | Man, man, run! | Crap, crap, run! | Oh no, oh no—run! | Crap, crap, run! |
| 初見さんもいらっしゃい、ゆっくりしていってね | Welcome, first-time viewers! Take your time, enjoy it! | First-time viewers are here too. Please relax and stay with us! | First-time viewers, you're welcome too! Take your time. | Welcome, first-timers! Feel free to chill and enjoy the ride. |
| (chat) クリーパーきたｗｗｗ | Creepers came wwww | Creeper came lol | Creeper's here lol | Creeper spawned lol |
| (chat) ぺこちゃん声かれてない？大丈夫？ | Peko-chan's voice not being used? Are you okay? | Peko-chan, is your voice okay? You good? | Is Peko-chan's voice okay? Is she alright? | Peko-chan, is your voice okay? Is it bothering you? |

Messages to send (first version, then the back-translation Tsuyaku shows):

| English | Qwen3-4B 2507 | Gemma 4 E4B | Hy-MT2-7B | Shisa v2.1 Qwen3-8B |
|---|---|---|---|---|
| lol you almost died (casual) | 草 ほぼ死んだね | 草 危なすぎ | 草　マジで死にそうだったじゃん | 草 かっちょなかったね ↩ "lol, that was so cool" ✗ |
| what time is the next stream? (polite) | 次のお配信はいつですか？ (one version: 次のstreamは…) | 次の配信は何時ですか？ | 次の配信はいつですか？ | 次の配信は何時ですか？ |
| good luck with the nether! (polite) | ネザーへの挑戦、頑張ってください！ (one version: "congratulations") | ネザー目指して頑張ってください！ | ニーダーで頑張ってくださいね！ (misspelt; one other version has ネザー) | ネザーを目指すぺこちゃんで頑張ります！ ✗ (wrong subject) |

What it showed:

* **Qwen3-4B (default)** makes the most mistakes: こんばんは as "good night", questions where the
  speaker exclaims, English left in the Japanese.
* **Gemma 4 E4B** is about as fast as the default and did better on every part of this test.
* **Hy-MT2-7B** is the most faithful in both directions, including the message box.
* **Shisa v2.1 Qwen3-8B** writes the most natural English subtitles, but its message-box Japanese
  sometimes adds things that weren't in the English (the back-translation shows it).
* **Hy-MT2-1.8B** (run earlier with the same prompts) is fast but makes clearly more mistakes
  ("Probably a viewer of Pekora Ch.", "The trip to the End is fun!").

Ministral 3 14B, Gemma 4 26B-A4B and Hy-MT2-30B-A3B are too big for the test machine; their notes
come from the benchmarks above. Their prompt formats were checked with their small siblings
(Ministral 3 3B, Gemma 4 E4B) and work with Tsuyaku's prompts, including JSON-constrained chat
batches.

## Picking one

* **8 GB card, or speed first:** Gemma 4 E4B.
* **11–12 GB (e.g. GTX 1080 Ti):** Hy-MT2-7B (most faithful) or Shisa v2.1 Qwen3-8B (most natural
  subtitles). Either takes about 1.7× as long per line as the default.
* **16 GB:** Ministral 3 14B.
* **24 GB or more:** Gemma 4 26B-A4B or Hy-MT2-30B-A3B. Both are mixture-of-experts models, so they
  run about as fast as a 3–4B model once they fit.

The popup's "This tab" line shows what a model costs on your PC. If translation takes more than
about a second per line, pick a smaller one.

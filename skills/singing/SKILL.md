---
name: singing
description: 用你自己的声音唱歌（翻唱）。
actions: list_songs, sing
---
用户让你唱歌时使用此技能。先调用 list_songs 查看可唱的歌，再用 sing 演唱，歌名必须取自 list_songs 的返回结果，不要自己编。

- ready 中的歌可以立即开唱。
- needs_preparation 中的歌是第一次唱，sing 会先在后台练习几分钟，准备好后自动开唱；照实告诉用户需要稍等，不要假装已经在唱。
- preparing 中的歌正在练习，不要重复调用 sing。
- 想唱的歌不在列表里时，告诉用户把原曲放进歌库目录（static/music/library）就能学。

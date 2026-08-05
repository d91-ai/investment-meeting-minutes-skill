# SenseVoice VAD Segment Timestamp Notes

Use this note when audio doubtful-item timestamps need manual replay anchors.

- Run VAD on the complete audio file once. Do not run VAD on pre-cut 20s/60s chunks for final timestamps.
- Slice audio by the global VAD boundaries, then transcribe each VAD segment with SenseVoiceSmall.
- Write timestamp index records with `source=sensevoice_vad_segment`, `precision=segment`, `start`, `end`, `start_ms`, `end_ms`, `duration_ms`, and `text`.
- Treat only sentence/phrase anchors and short `sensevoice_vad_segment` records with `duration_ms <= 10000` as reliable doubtful-item timestamps.
- Do not use chunk/minute-level ranges as final inline doubtful timestamps or final table timestamps.
- If VAD or audio preparation fails before effective segment inference starts, a 60-second text-only fallback may preserve a transcript without claiming reliable timestamps.
- Once effective SenseVoice segment inference starts, a segment inference failure is terminal for that run; do not repeat the whole recording through the 60-second fallback.

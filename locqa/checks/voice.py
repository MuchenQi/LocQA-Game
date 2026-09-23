"""Character voice and register.

Japanese marks speaker identity far more heavily than English does. A character
is characterized by their 一人称 (first-person pronoun), their 語尾 (sentence-final
particles), and their politeness level — none of which the English source
specifies. A translator working through a spreadsheet, string by string, out of
narrative order, drifts. The English reviewer cannot see it, because English has
no surface form to drift.

These checks read the whole file at once and flag the minority form per speaker.
They do not decide what a character's voice should be; they detect that it
changed.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Iterable

from ..model import Finding, Script, Segment, Severity
from .base import Check, Config, register
from .japanese import _strip_markup

# 一人称 — role language markers. Order matters: check longer forms first.
FIRST_PERSON = [
    ("わたくし", "formal, deferential"),
    ("あたし", "feminine, casual"),
    ("わたし", "neutral polite"),
    ("私", "neutral (reading ambiguous)"),
    ("ぼく", "boyish, soft"),
    ("僕", "boyish, soft"),
    ("おれ", "rough, masculine"),
    ("俺", "rough, masculine"),
    ("うち", "regional, feminine-casual"),
    ("わし", "elderly, masculine"),
    ("拙者", "archaic, samurai register"),
]

# 語尾 — sentence-final particles that carry persona.
FINAL_PARTICLES = ["ぜ", "ぞ", "わ", "のよ", "かしら", "だい", "かい", "ですわ", "じゃ", "のう", "にゃ", "っす"]

# Sentence-final particles (ね・よ・な・か・の・さ・わ) attach after the polite
# suffix without changing the register, so they must be allowed to trail.
POLITE_ENDINGS = re.compile(
    r"(?:です|ます|ません|でした|ました|でしょう|ましょう|ください|ございます)"
    r"[ねよなかのさわ]*[。！？…\s]*$"
)
# Plain form is harder to detect than polite form: です・ます are closed-class
# suffixes, while 常体 is just the dictionary form of the verb. The last
# alternative catches 終止形 verbs (ある / 行く / 見る), with lookbehind excluding
# ます・です and the interjections そう・こう・どう・ほう, which end in the same
# vowel row but carry no register.
PLAIN_ENDINGS = re.compile(
    r"(?:だ|だった|である|だろう|じゃない|ない|なかった|かった|してる|してた|するさ"
    # Plain past 〜た is the default ending of Japanese narration, and leaving
    # it out classified every past-tense line as NEUTRAL — the single largest
    # source of error in the classifier, found by eval/ rather than by the
    # tests. The lookbehinds hand まし／でし back to POLITE_ENDINGS, which is
    # checked first anyway, so that a genuinely mixed line still reads MIXED.
    r"|(?<!まし)(?<!でし)た"
    r"|よ|ぜ|ぞ|(?<![まちでそこどほ])[うくぐすつぬぶむる]"
    # i-adjective predicates (遅い / 高い) are plain form. The lookbehind drops
    # はい・おい・ええい, which end in the same kana but are interjections.
    r"|(?<![はおえ])(?<=[\u4E00-\u9FFF\u3041-\u309F])い)[ねよなかのさわ]*[。！？…\s]*$"
)

SENTENCE_SPLIT = re.compile(r"(?<=[。！？])")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text) if s.strip()]


def classify_register(text: str) -> str:
    """POLITE / PLAIN / NEUTRAL for one target string.

    NEUTRAL covers 体言止め (noun-final), interjections, and fragments — which
    are extremely common in game dialogue and carry no register information.
    Counting them as either form is the main way naive register checks
    generate noise, so they are excluded rather than guessed at.
    """
    polite = plain = 0
    for sent in sentences(text):
        if POLITE_ENDINGS.search(sent):
            polite += 1
        elif PLAIN_ENDINGS.search(sent):
            plain += 1
    if polite and not plain:
        return "POLITE"
    if plain and not polite:
        return "PLAIN"
    if polite and plain:
        return "MIXED"
    return "NEUTRAL"


def _speaker_groups(script: Script) -> dict[str, list[Segment]]:
    groups: dict[str, list[Segment]] = defaultdict(list)
    for seg in script:
        if seg.speaker and seg.is_translated and not seg.is_choice:
            groups[seg.speaker].append(seg)
    return groups


@register
class RegisterConsistency(Check):
    id = "voice.register"
    tier = 2
    title = "Politeness register drift"
    description = "A speaker switches between です・ます and plain form with no narrative reason."
    default_severity = Severity.MAJOR

    MIN_LINES = 4
    MIN_RATIO = 0.8

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        if not config.enforce_register:
            return
        for speaker, segs in _speaker_groups(script).items():
            classified = [(s, classify_register(s.target)) for s in segs]
            informative = [(s, r) for s, r in classified if r in ("POLITE", "PLAIN")]
            if len(informative) < self.MIN_LINES:
                continue
            counts = Counter(r for _, r in informative)
            majority, majority_n = counts.most_common(1)[0]
            if majority_n / len(informative) < self.MIN_RATIO:
                continue                    # genuinely mixed speaker; not our call
            for seg, register in informative:
                if register != majority:
                    yield self.finding(
                        config,
                        f"{speaker} speaks in {_jp(majority)} in {majority_n} of "
                        f"{len(informative)} classified lines, but this line is {_jp(register)}.",
                        segment=seg,
                        excerpt=seg.target,
                        suggestion="If the shift is deliberate — a character dropping formality at a "
                        "turning point — mark the segment to suppress this check.",
                        meta={"speaker": speaker, "expected": majority, "found": register},
                    )
            for seg, register in classified:
                if register == "MIXED":
                    yield self.finding(
                        config,
                        f"{speaker} mixes polite and plain form within a single line.",
                        segment=seg,
                        excerpt=seg.target,
                        severity=Severity.MINOR,
                        meta={"speaker": speaker},
                    )


@register
class FirstPersonConsistency(Check):
    id = "voice.first_person"
    tier = 2
    title = "First-person pronoun drift"
    description = "A speaker's 一人称 changes mid-script, which reads as a different character."
    default_severity = Severity.MAJOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for speaker, segs in _speaker_groups(script).items():
            found: dict[str, list[Segment]] = defaultdict(list)
            for seg in segs:
                pronoun = _first_person(seg.target)
                if pronoun:
                    found[pronoun].append(seg)
            if len(found) < 2:
                continue
            ranked = sorted(found.items(), key=lambda kv: -len(kv[1]))
            dominant, dominant_segs = ranked[0]
            if len(dominant_segs) < 2:
                continue
            for pronoun, outliers in ranked[1:]:
                note = dict(FIRST_PERSON).get(pronoun, "")
                for seg in outliers:
                    yield self.finding(
                        config,
                        f"{speaker} uses 「{pronoun}」 ({note}) here but 「{dominant}」 in "
                        f"{len(dominant_segs)} other lines.",
                        segment=seg,
                        excerpt=seg.target,
                        suggestion=f"Unless the character is quoting someone, use 「{dominant}」.",
                        meta={"speaker": speaker, "found": pronoun, "dominant": dominant},
                    )


@register
class FinalParticleDrift(Check):
    id = "voice.final_particle"
    tier = 2
    title = "Sentence-final particle drift"
    description = "A persona-marking 語尾 appears for a speaker who otherwise never uses it."
    default_severity = Severity.MINOR

    def run(self, script: Script, config: Config) -> Iterable[Finding]:
        for speaker, segs in _speaker_groups(script).items():
            if len(segs) < 6:
                continue
            usage: dict[str, list[Segment]] = defaultdict(list)
            for seg in segs:
                for particle in FINAL_PARTICLES:
                    if re.search(re.escape(particle) + r"[。！？…]*$", _strip_markup(seg.target).strip()):
                        usage[particle].append(seg)
            for particle, hits in usage.items():
                # One appearance across a large speaker set is the signal:
                # either the persona marker was forgotten everywhere else, or
                # it was added here by mistake.
                if len(hits) == 1 and len(segs) >= 8:
                    yield self.finding(
                        config,
                        f"{speaker} ends a line with 「{particle}」 exactly once across "
                        f"{len(segs)} lines.",
                        segment=hits[0],
                        excerpt=hits[0].target,
                        suggestion="Either the persona marker is inconsistent, or it belongs to another character.",
                        meta={"speaker": speaker, "particle": particle},
                    )


def _first_person(text: str) -> str | None:
    text = _strip_markup(text)
    for pronoun, _ in FIRST_PERSON:
        if pronoun in text:
            return pronoun
    return None


def _jp(register: str) -> str:
    return {"POLITE": "です・ます調", "PLAIN": "だ・である調"}.get(register, register)

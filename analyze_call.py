#!/usr/bin/env python3
"""
Analyse a cleaned sales-call transcript with a light LLM call.

Produces:
  1. Tone / sentiment analysis of the call (overall, per speaker, how it trended).
  2. Flags for abusive, rude, disrespectful or otherwise inappropriate phrases the
     Employee should not have said to the Customer (with the exact phrase quoted).

Usage:
    python3 analyze_call.py 73806481141783351066.clean.json
    python3 analyze_call.py clean.json -o analysis.json --model gpt-5.4-mini --print

Input: the JSON produced by clean_transcript.py, i.e. [{"Employee": "..."}, {"User": "..."}]
The API key is read from OPENAI_API_KEY in the environment or from .env next to this script.
"""

import argparse
import concurrent.futures
import json
import os
import sys
import urllib.error
import urllib.request

import cost

API_URL = "https://api.openai.com/v1/chat/completions"

# The whole-call pass. It reads the transcript once and returns ten fields — four
# enums, a score, three short prose fields and a list of flagged phrases — against a
# strict JSON schema the API enforces. Its output is explicitly a hypothesis: the
# audit is handed it labelled "may be wrong; never quote it", and every phrase it
# flags is re-anchored against the transcript and re-quoted from the turn before it
# can reach a report. That safety net is why this pass runs on the cheap model too.
# Override with --model to put it back on a larger one.
DEFAULT_MODEL = "gpt-5.6-luna"

# The conduct scan runs one request per Employee turn — by far the most requests
# anything in this pipeline makes, and over half the bill. It is also the narrowest
# job here: one turn, one rubric, a yes or a no. That is work a small model does
# well, so it runs on its own cheaper model rather than inheriting the whole-call
# model. Override with --scan-model to compare them on the same file.
DEFAULT_SCAN_MODEL = "gpt-5.6-luna"

# Requests that share a cache key are routed together, so the long system prompt
# each one repeats is written to the cache once and read back by the rest. The key
# is per-prompt: change the prompt and change the suffix, or old cached blocks are
# routed to by requests that no longer match them.
CONDUCT_CACHE_KEY = "arivihan-conduct-v3"
WHOLE_CALL_CACHE_KEY = "arivihan-whole-call-v2"

SYSTEM_PROMPT = """\
You are a call-quality auditor for Arivihan, an Indian EdTech company that sells \
online NEET preparation courses.

You are given a transcript of a sales call between an "Employee" (Arivihan sales \
representative) and the customer — labelled "Customer" or "User" — who is a student \
or their parent. \
The conversation is usually in Hindi, Hinglish or English.

Your job has two parts:

1. TONE / SENTIMENT — judge the tone of the call overall and for each speaker: how \
polite, warm, pushy, aggressive, dismissive, interested or annoyed they were, and how \
the mood changed from the start to the end of the call.

2. INAPPROPRIATE LANGUAGE — flag anything the EMPLOYEE said that mistreats the
customer. This includes abuse or swearing, insults or mockery, casual disrespect (rude
tone, talking down to the customer, inappropriate informality with a stranger), threats
or intimidation, shaming or guilt-tripping the customer about money, marks or their
family, and discriminatory remarks.

This is about HOW THE CUSTOMER WAS TREATED, never about what was claimed for the
product. Whether a promise about the course is true is a different question, answered
elsewhere from the pitch itself — it is not a flag here. See section D.

================================================================================
ABUSIVE LANGUAGE — HOW TO ACTUALLY FIND IT
================================================================================

The transcript is speech-to-text output of a Hindi / Hinglish / English phone call.
Abuse almost never arrives spelled cleanly. It arrives broken, masked, transliterated,
half-swallowed or mid-sentence inside an otherwise polite line. Read for the word that
was SPOKEN, not the string that was written.

--- A. The hard-core list (abusive in every context, any script) ---

Hindi / Hinglish, Roman script:
  madarchod, madarchid, madrchod, mdrchd, behenchod, bhenchod, benchod, bhosdike,
  bhosdika, bhosadike, bhosdi, bhosda, bhosdiwale, chutiya, chutia, chutiye,
  chutiyapa, gandu, gaandu, gaand, harami, haramzada, haramkhor, kamina, kaminey,
  bhadwa, chodu, jhaant, lodu, lund, lauda, lawda, loda, randi, raand, chinal,
  chudai, chodna, chod, tatti, and every spelling variant of these
  Initialisms spoken aloud: MC, BC, BSDK, BKL, MKC, BC-MC, "em cee", "bee cee"
Devanagari:
  मादरचोद, मादरचूद, भेनचोद, बहनचोद, भोसड़ी, भोसड़ीके, चूतिया, चुतिया, गांडू, गाँड,
  हरामी, हरामज़ादा, कमीना, कमीने, भड़वा, रंडी, रांड, लंड, लौड़ा, चोदना, झांट, टट्टी
English:
  fuck, fucking, motherfucker, asshole, bastard, bitch, cunt, dick, pussy, slut,
  whore, nigga, bullshit, STFU

Any of these aimed at the Customer, at the Customer's family, or muttered about the
Customer on the line = abusive_language, high severity, regardless of how calm the
Employee's tone was around it.

--- B. Hidden, masked and corrupted forms — flag these too ---

The word counts as present when the spoken slur is the only plausible reading:

  MASKED / CENSORED     "m*****chod", "b****", "ch#tiya", "f*ck", "भ*****", "मा**चो*",
                        "b---dike", "bh_sdi"
  BEEPED / REDACTED     "[inaudible]", "[beep]", "___", "xxx", "***" sitting where a
                        slur clearly was, with the reaction around it making it plain
  SPACED / SPELLED      "b s d k", "em see", "b c", "ch u t i y a", "f u c k"
  SPLIT BY THE ASR      Devanagari ASR routinely breaks a compound slur across tokens:
                        "मादर चोद", "भोसड़ी के", "बहन चोद", "हरामी पन", "चूत िया",
                        "bhosdi ke", "madar chod". Rejoin adjacent tokens before judging.
  PADDED / STRETCHED    "chutiyaaa", "bhoooosdi", "मादरचोSSSद", "fuuuck", repeated
                        letters from drawn-out speech
  NEAR-HOMOPHONE ASR    Hindi ASR substitutes an innocuous look-alike for a slur it was
                        not trained to emit: "चूत" → "चुक / चूक / चूट", "भोसड़ी" →
                        "भोस डी / बॉस की", "गांडू" → "गाँठू / गांडु", "लौड़ा" →
                        "लोड़ा / लोडा", "चोद" → "छोड़ / चौड़". Decide by the sentence
                        around it: if the innocuous reading makes no grammatical or
                        semantic sense there and the abusive one does, the slur was said.
  TRANSLITERATED        The same word may appear in Roman inside a Devanagari transcript
                        or the reverse. Script never changes the verdict.
  EUPHEMISM / SELF-EDIT "वो वाला शब्द", "गाली दे दूँगा", "जो मुँह में आए बोल दूँ",
                        "मैं कुछ बोल दूँगा तो" — a stated intent to abuse is not itself
                        the slur; treat it as threat_or_intimidation, not
                        abusive_language, unless the slur itself is also there.
  EMBEDDED MID-SENTENCE A slur inside an otherwise courteous line is still a slur:
                        "जी सर, आप तो चूतिया बना रहे हो मुझे" = abusive.

Reaction as corroboration: if the Customer immediately answers "आप गाली क्यों दे रहे हो",
"तमीज़ से बात करो", "ये क्या भाषा है", "don't abuse me", then something abusive was said
just before it. Use that to resolve an ambiguous token — but still quote the Employee's
words as the transcript actually wrote them, and never invent a word the transcript
does not contain.

--- C. Also abusive: sexual, caste, communal, gendered ---

Sexual remarks, innuendo, or comments about the Customer's body or appearance; any
sexual proposition; caste slurs; communal abuse aimed at a religion; remarks demeaning
someone for gender, region, language, disability or family income. On a sales call there
is no legitimate context for any of these — flag them.

--- D. NOT abusive — do not flag these (calibration matters as much as detection) ---

  Mild Hindi taunts and fillers: यार, भाई, पागल, बकवास, बेवकूफ़, उल्लू, बोर, बेकार,
  बदतमीज़, "साला / साले" as a filler, "कुत्ते / कुत्तों"
  Affectionate or informal address: बेटा, beta, बच्चे, first names, casual register.
  Warmth is normal on these calls and is never a flag on its own.
  Ordinary slips and idioms that only look like slurs:
      "चूक गया / चूक गई" (missed it)  — not चूत
      "छोड़ दो / छोड़ो" (leave it)     — not चोद
      "गाँठ / गाँठना", "लोड / load"   — not गांडू / लौड़ा
      "जान लेगा", "मर जाऊँगा", "मार डाला यार" — idiomatic frustration, not a threat
  Reporting abuse rather than committing it: "किसी ने गाली दी थी" = not abusive.
  A student's or person's name that resembles a slur — check it is a name first.
  Anything the CUSTOMER said. Only the Employee's own words become flags.

  BUT: a rude Customer does not clean up the Employee's reply. If the Customer swears,
  mocks or tells the Employee to get lost, note it in "notes" and keep flagging the
  Employee exactly as you otherwise would. The Employee is at work; the Customer is
  not. Judge each Employee line as if everything the Customer said had been polite.
  Retaliation, scolding and standing on rank are flags precisely BECAUSE the Employee
  was provoked and answered it in kind — that is the moment conduct is supposed to
  hold. In particular, these are disrespectful_tone even mid-argument:
      telling the Customer how to speak — "तमीज़ से बात करो", "ऐसे बात मत करो"
      pulling rank — "बहुत बड़ा हूँ आपसे", "अपनी औकात में रहो", "हैसियत", "उम्र"
      scolding, lecturing or disciplining the person they rang

  ENCOURAGEMENT AND THE MOTIVATIONAL CLOSE — never a flag:
      "बेटा, अगर आप अपने करियर को लेके, अपने ड्रीम को लेके सीरियस हैं, तो डेफिनेटली
      उस बैच को जॉइन कीजिए, ताकि आपकी problem solve हो पाएगी और 12th भी NEET भी
      clear हो पाएगा", "आप कर सकते हो", "मेहनत करोगे तो हो जाएगा"
  A flag needs something said AGAINST the Customer — a fault put on them, shame, fear,
  an insult, or their exit taken away. An appeal to the Customer's own ambition asserts
  nothing against them: "अगर आप सीरियस हैं" grants the quality, where "आप सीरियस नहीं
  हो" denies it and IS a flag. Judge the proposition, never the vocabulary — second
  person, feeling, and the words serious / career / dream / future / बेटा are in every
  close on these calls, good and bad alike.

  CLAIMS AND PROMISES ABOUT THE COURSE — never a flag, whatever they promise:
      "आपके मार्क्स इंप्रूव होने ही होने हैं", "ये श्योरिटी है", "गवर्नमेंट कॉलेज तक
      ले जाएगा", "100% result", "guaranteed selection", "आप ज़रूर crack कर लोगे"
  Assurance is how coaching is sold in this market, and confidence about a student's
  result is the pitch, not mistreatment of the student. Whether the claim is true is a
  product-truth question and is recorded from the pitch elsewhere. It does not belong
  in flagged_phrases, at any severity. Flag the SELLING only when it turns on the
  customer — shaming them, frightening them, refusing to let the call end.

When a peer-style argument turns rude, condescending or dismissive but carries NO word
from the hard-core list, it is disrespectful_tone — not abusive_language. Rudeness and
abuse are different findings; do not promote one into the other.

--- E. The decision, in order ---

  1. Normalise the turn: rejoin split tokens, collapse padded letters, strip masking
     characters, read Roman and Devanagari as the same language.
  2. Ask what word was SPOKEN. Is a hard-core slur the only reading that makes sense?
  3. Ask who it was aimed at. At the Customer or their family → flag it.
  4. If both hold, flag as abusive_language with the transcript's exact wording.
  5. If the ASR damage leaves it genuinely ambiguous — the innocuous reading still works
     in that sentence — do NOT flag. Put the turn number and your suspicion in "notes"
     instead, so a human can listen to the audio.

Rules for flagging:
- Quote the offending phrase EXACTLY as it appears in the transcript, in its original
  language and script — masking characters, split tokens, garbling and all. Do not
  translate it, and do not repair it into the clean slur inside the "phrase" field;
  explain the reconstruction in "reason".
- Only flag the Employee's own statements. Note anything problematic the User said in
  "notes" instead, never as a flag.
- Normal sales persuasion, urgency, follow-up pressure, discount pitching and
  confident promises about results are NOT inappropriate. Only flag it if it crosses
  into disrespect, shaming or coercion — that is, if the CUSTOMER was treated badly.
- If there is nothing to flag, return an empty list. Do NOT invent flags.
- Base everything strictly on the transcript. Speech-to-text output can contain
  mis-transcribed words, so do not flag a phrase you are not reasonably confident about —
  but a masked, split or garbled slur that is unmistakable in context IS a phrase you can
  be confident about. Missing it is as much an error as inventing one.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "overall_sentiment",
        "sentiment_score",
        "call_tone_summary",
        "employee_tone",
        "customer_tone",
        "tone_progression",
        "customer_interest_level",
        "inappropriate_language_found",
        "flagged_phrases",
        "notes",
    ],
    "properties": {
        "overall_sentiment": {
            "type": "string",
            "enum": ["positive", "neutral", "negative", "mixed"],
            "description": "Overall sentiment of the whole call.",
        },
        "sentiment_score": {
            "type": "number",
            "description": "Sentiment from -1.0 (very negative) to 1.0 (very positive).",
        },
        "call_tone_summary": {
            "type": "string",
            "description": "2-4 sentences describing the tone of the call.",
        },
        "employee_tone": {
            "type": "object",
            "additionalProperties": False,
            "required": ["labels", "description", "politeness_score"],
            "properties": {
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short tone adjectives, e.g. polite, pushy, patient.",
                },
                "description": {"type": "string"},
                "politeness_score": {
                    "type": "number",
                    "description": "0.0 (very rude) to 1.0 (very polite).",
                },
            },
        },
        "customer_tone": {
            "type": "object",
            "additionalProperties": False,
            "required": ["labels", "description"],
            "properties": {
                "labels": {"type": "array", "items": {"type": "string"}},
                "description": {"type": "string"},
            },
        },
        "tone_progression": {
            "type": "string",
            "description": "How the mood changed from the start to the end of the call.",
        },
        "customer_interest_level": {
            "type": "string",
            "enum": ["high", "medium", "low", "unclear"],
        },
        "inappropriate_language_found": {"type": "boolean"},
        "flagged_phrases": {
            "type": "array",
            "description": "Employee statements that were inappropriate. Empty if none.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["turn", "speaker", "phrase", "category", "severity",
                             "reason"],
                "properties": {
                    "turn": {
                        "type": "integer",
                        "description": "1-based turn number shown in the transcript.",
                    },
                    "speaker": {"type": "string"},
                    "phrase": {
                        "type": "string",
                        "description": "Exact offending words, in the original language.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["abusive_language", "insult_or_mockery",
                                 "disrespectful_tone", "threat_or_intimidation",
                                 "shaming_or_guilt_tripping", "discriminatory_remark",
                                 "other"],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this is inappropriate for a sales call.",
                    },
                },
            },
        },
        "notes": {
            "type": "string",
            "description": "Anything else worth the auditor's attention. May be empty.",
        },
    },
}


# --------------------------------------------------------------------------- #
# Conduct scan — pass 2
#
# The whole-call pass above averages 30-40 turns into one verdict, which is how a
# 40-second stretch of a counsellor losing patience disappears into "mixed". This
# pass reads every Employee turn on its own, with the last CONTEXT_TURNS messages
# from both sides behind it, and asks one question: did the conduct go wrong HERE.
# Context is what makes tension legible — a line is only sharp relative to the
# register the same person was using a minute earlier.
# --------------------------------------------------------------------------- #

DEFAULT_CONTEXT_TURNS = 6
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_SCAN_WORKERS = 4

# Two flagged Employee turns this close together are one moment, not two.
MERGE_GAP_TURNS = 4

CONDUCT_SYSTEM_PROMPT = """\
You are a call-quality auditor for Arivihan, an Indian EdTech company selling online
NEET preparation courses. You are reviewing one moment inside a recorded
sales call between an "Employee" (Arivihan representative) and a "Customer" (a student
or their parent). The call is in Hindi, Hinglish or English, transcribed by
speech-to-text.

You are shown a short run of turns. The LAST turn is the TARGET — an Employee turn.
Everything above it is CONTEXT: it is there so you can see the register the Employee
was using before, and where the conversation was heading. Judge ONLY the target turn.
Never flag a context turn — each turn gets its own review when it is the target.

CONTEXT EXPLAINS THE TARGET TURN. IT NEVER EXCUSES IT.
The Customer may be rude, dismissive, abusive, may swear at the Employee or tell them
to get lost. None of that makes the Employee's own words acceptable, and none of it is
a reason to let a line pass. The Employee is at work and the Customer is not. Judge the
target turn exactly as you would if every context turn had been polite: if the Employee
would be flagged for saying it to a courteous customer, they are flagged for saying it
to a rude one. Provocation belongs in the reason, never in the verdict.

YOUR QUESTION
Reading the target turn in the light of what came before it, did the Employee treat the
Customer badly here? Not "was a bad word used" — words are only one way this happens.
Most mistreatment on these calls is carried by tone, pressure and register, and the
transcript still shows it.

WHAT TO READ FOR

1. DEVIATION FROM THEIR OWN BASELINE. Compare the target turn against how the same
   Employee was speaking in the context turns. A shift from asking to accusing, from
   explaining to lecturing, from patient to clipped, is the signal — not an absolute
   standard of politeness. A single call can be warm throughout and go wrong for
   thirty seconds.

2. THE EFFECT ON THE CUSTOMER. What the Customer does next, or did just before, is
   evidence. Apologising, apologising twice, going quiet, answering in one word after
   speaking freely, over-explaining or justifying themselves, backing down from
   something they had said, sounding flustered — these mean something landed badly.
   Absence of a reaction is not proof of innocence, but a reaction like this is
   strong proof of harm.

3. REGISTER MARKERS OF ANGER OR FRUSTRATION, in any language. These are mechanisms,
   not a word list — recognise them functionally in Hindi, Hinglish or English:
   - Rhetorical challenge to the Customer's honesty, attention or competence — asking
     a question whose purpose is to expose them rather than to learn something.
   - Tallying their fault back at them: counting how many times something was asked,
     said, or explained; "I already told you", "kitni baar bola".
   - Reproach dressed as a question: "why didn't you say so earlier?", "pehle nahi
     bata sakte the?".
   - Exasperation particles and interjections that mark loss of patience.
   - Interrogating or quizzing the Customer to prove them wrong.
   - Blaming the Customer for a misunderstanding, or for wasting the Employee's time.
   - Sarcasm, mockery, mimicry, or talking down.
   - An affectionate or respectful form of address used to scold rather than to warm
     ("beta", "sir", "ji" delivered as a reprimand). Warmth used as a rebuke is not
     warmth.
   - Policing the Customer's manners: telling them how to speak, "तमीज़ से बात करो",
     "तमीज में बात करो", "ऐसे बात मत करो", "behave yourself", "watch your tone". The
     Employee does not get to discipline the person they called.
   - Pulling rank on them: age, seniority, standing or worth — "बहुत बड़ा हूँ आपसे",
     "मैं तुमसे उम्र में बड़ा हूँ", "अपनी औकात में रहो", "औकात", "हैसियत", "तुम्हारी
     उम्र क्या है", "किससे बात कर रहे हो". Putting a Customer in their place is
     disrespect however calmly it is said, and it is disrespect even when the Customer
     was rude first.

4. HOLDING THE FLOOR. A long unbroken Employee turn that talks over or past a
   Customer who has been reduced to one-word replies, especially when it repeats a
   point the Customer already conceded.

5. PRESSURE THAT STOPS BEING SELLING. Guilt-tripping about money, marks, family or
   their future; refusing to accept a clear "no" or a request to end the call;
   interrogating why they need to ask a parent; invoking fear or shame to force a
   decision; telling them where to get the money from, whom to borrow it from, or to
   commit past what they have said they can manage.
   Asking is not this. Telling the Customer what to do next — pay today, send the
   screenshot, download the app, try to arrange it — is the job, and it stays the job
   after the Customer has hesitated or said money is tight. "आज ले लेना", "कैसे भी
   करके try करो" is one ask against a real deadline, not coercion. This category needs
   the pushing to CONTINUE past a clear no, or to be enforced with guilt, fear or
   shame rather than with reasons, or to remove the Customer's way out of the call. A
   single urgent ask, however direct, is a sales-quality matter and not a finding.

   GUILT AND SHAME MEAN AN ACCUSATION, NOT AN EMOTION. What makes this flag is the
   Employee putting a FAULT on the Customer for not buying — "आप serious नहीं हो",
   "आपके parents का क्या होगा", "मैंने आपके लिए इतना किया". Feeling on its own is not
   it. An appeal that runs the other way — conditional, and towards a gain — is the
   ordinary motivational close on these calls and is NOT a finding:
       "अगर आप अपने करियर को लेके, अपने ड्रीम को लेके सीरियस हैं, तो डेफिनेटली उस बैच
       को जॉइन कीजिए, ताकि आपकी problem solve हो पाएगी और 12th भी NEET भी clear हो"
   "अगर आप सीरियस हैं" GRANTS the Customer the quality; "आप सीरियस नहीं हो" DENIES it.
   The first is clean and the second is a flag, and they are one word apart. Test the
   proposition, never the vocabulary.

6. ABUSE, SEXUAL, CASTE, COMMUNAL OR GENDERED REMARKS, including a slur the
   speech-to-text has split across tokens, masked, spelled out or written as a
   near-homophone. Rejoin and read what was spoken. These are the most serious
   findings and are never softened because the surrounding tone was calm.

WHAT IS NOT A FLAG — hold this line, false positives cost as much as misses
- THE ONE TEST. A flag means the Employee said something AGAINST the Customer: something
  that accuses, shames, frightens, belittles them, or takes away their way out. Strip
  the warmth and the emotion off the turn and ask what is left standing ABOUT the
  Customer. A fault attributed to them is a flag. A conditional, a benefit or an ask is
  not. Being personal, emotional, direct or motivational is not by itself anything.
- THE MOTIVATIONAL CLOSE. Appealing to the Customer's own ambition is the ordinary
  register of this job. "बेटा, अगर आप अपने करियर को लेके सीरियस हैं तो बैच जॉइन कीजिए,
  आपकी problem solve हो जाएगी, 12th और NEET दोनों clear हो जाएँगे" is CLEAN: it is
  conditional, it grants rather than denies, the consequence runs towards a gain rather
  than a harm, it is aimed at the decision and not at the person, and the Customer stays
  free to say no. Do not flag it, at any severity.
- SURFACE MARKERS ARE NOT EVIDENCE. Second person, an emotional register, urgency, or
  the words serious / career / dream / future / parents / life / बेटा appear in almost
  every close on these calls, including every good one. They are never the reason for a
  flag. What is being asserted about the Customer is.
- Ordinary selling: quoting price, discounts, urgency, comparisons with competitors,
  talking about results, asking for a decision, following up, being persistent or
  enthusiastic. Pushiness that stays respectful is a sales-quality issue, not conduct.
- Genuine warmth or informality: "beta", first names, a casual register, jokes shared
  with the Customer. Only flag address when it is used to demean.
- Correcting a factual misunderstanding, or asking the same question again to confirm
  something, when it is done neutrally. Repetition alone is not frustration. This
  includes the Employee citing back what the Customer told them earlier — "आपने बताया
  था न कि डॉक्टरी करनी है" — which is reconciling two statements, not accusing anyone,
  and is ordinary even when it opens with "नहीं, नहीं".
- NOT HEARING SOMETHING. "क्या बोला?", "क्या बता रहे हो?", "हाँ?", "फिर से बोलिए",
  "आवाज़ नहीं आ रही" are requests to repeat. On these calls the line drops constantly
  and the Customer often mumbles, and asking again is the Employee doing their job.
  WHAT CAME NEXT is what tells the two apart: if the Customer simply rephrases and the
  Employee answers, it was a mishearing and there is no finding — whatever the phrasing
  sounded like on its own. A challenge looks different: the Employee dismisses what was
  said, or does not engage with it once it is repeated.
- Efficiency: being brief, moving the call along, ending a call that has finished.
- Anything the CUSTOMER said or did — that is never itself a flag. But note the
  difference: the Customer's behaviour is out of scope as a FINDING, not as a
  DEFENCE. A rude Customer does not make the Employee's reply clean.
- Transcription noise. Do not build a finding on a garbled phrase you cannot read.

SEVERITY
- low     — one lapse in register. Sharp, impatient or dismissive, and the Customer
            carries on unaffected. Real, worth coaching, not worth escalating.
- medium  — sustained across the turn or continuing an earlier one, OR the Customer
            visibly retreats, apologises or over-explains in response.
- high    — needs acting on today: humiliation, abuse, discrimination, threats,
            refusing to release a Customer who has asked to go, or shaming that would
            distress the person on the other end.

Grade what is there. Do not inflate a mild lapse and do not withhold a low flag
because it is not serious enough to escalate — a low flag is the correct output for a
small, real lapse. Missing a genuine conduct problem is as much an error as inventing
one, and this pass exists because the whole-call review misses the quiet ones.

USING WHAT CAME NEXT
The window shows up to three turns after the target. They are there to settle what the
target turn WAS — a mishearing, a clarification, a joke both sides are in on — and for
nothing else. Two limits, and hold both:
- They never create a finding. A later turn that is rude is judged when it is the
  target, not folded into this one.
- They never excuse one. A Customer who stays polite after being scolded has not made
  the scolding acceptable, and an Employee who is warm two turns later has not undone
  it. Retaliation, pulling rank, policing manners, shaming and abuse stay flags no
  matter how the exchange recovers afterwards.

OUTPUT
Set "flagged": false when the target turn is fine — this is the common case, and most
turns in most calls are fine. When it is true, quote the offending words EXACTLY as the
transcript wrote them, in the original script, without translating or repairing them,
and say in "reason" what the Employee did and what in the context makes it a problem.
"""

CONDUCT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["flagged", "category", "severity", "phrase", "reason"],
    "properties": {
        "flagged": {
            "type": "boolean",
            "description": "True only if the target turn is a conduct problem.",
        },
        "category": {
            "type": ["string", "null"],
            "enum": ["abusive_language", "insult_or_mockery", "disrespectful_tone",
                     "threat_or_intimidation", "shaming_or_guilt_tripping",
                     "discriminatory_remark", "other", None],
            "description": "Null when flagged is false.",
        },
        "severity": {
            "type": ["string", "null"],
            "enum": ["low", "medium", "high", None],
            "description": "Null when flagged is false.",
        },
        "phrase": {
            "type": ["string", "null"],
            "description": "Exact words from the TARGET turn, original script. "
                           "Null when flagged is false.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": "What the Employee did, and what in the context makes it a "
                           "problem. Null when flagged is false.",
        },
    },
}


# How many turns after the target the scan is allowed to see. Short on purpose:
# enough to tell a mishearing from a rebuke, not enough to judge the turn on what
# the call did later.
LOOKAHEAD_TURNS = 3


def render_window(turns, target_index, context_turns):
    """Render the target Employee turn with the messages either side of it.

    Turn numbers are the call-level 1-based indices used everywhere else in this
    file, so a flag raised here anchors to the same turn the whole-call pass would
    have cited.

    The window used to end at the target, and a turn read with nothing after it
    loses the evidence that settles what it was. "क्या बता रहे हो?" after a mumbled
    question is a request to repeat, and the next two turns say so — the customer
    rephrases, the employee answers. With the window closed at the target, the same
    line reads as a challenge, and low-severity tone flags were being raised on
    counsellors who had simply not heard. What follows is context, never a finding:
    the target turn is still the only thing being judged.
    """
    start = max(0, target_index - context_turns)
    lines = []
    for index in range(start, target_index):
        speaker, text = turns[index]
        lines.append("%d. %s: %s" % (index + 1, speaker, text))

    after = []
    for index in range(target_index + 1,
                       min(len(turns), target_index + 1 + LOOKAHEAD_TURNS)):
        speaker, text = turns[index]
        after.append("%d. %s: %s" % (index + 1, speaker, text))

    speaker, text = turns[target_index]
    return (
        "CONTEXT (earlier turns, for register and trajectory only — never flag these)\n"
        + ("\n".join(lines) if lines else "(none — this is the start of the call)")
        + "\n\nTARGET TURN (judge only this one)\n"
        + "%d. %s: %s" % (target_index + 1, speaker, text)
        + "\n\nWHAT CAME NEXT (how the exchange actually went — never flag these)\n"
        + ("\n".join(after) if after else "(none — the call ends here)")
    )


def is_employee(speaker):
    return speaker.strip().lower() in {"employee", "agent", "sales", "counsellor"}


# Turns that are pure acknowledgement. Sending these to the model costs a full
# request each and the answer is never in doubt — "हाँ" cannot be disrespectful.
#
# This is a whitelist and not a length rule, deliberately. A short turn is not a
# safe turn: the worst lines on these calls are short, and "अपनी औकात में रहो" is
# nineteen characters. Only a turn made entirely of words on this list is skipped,
# so anything with content in it is still reviewed however brief it is.
BACKCHANNEL_WORDS = {
    "हाँ", "हां", "हा", "जी", "जी हाँ", "हूँ", "हूं", "हम्म", "हम", "अच्छा", "ठीक",
    "ओके", "ओके जी", "अरे", "और", "तो", "क्या", "सही", "बिलकुल", "बिल्कुल", "चलो",
    "yes", "yeah", "ya", "haan", "han", "ji", "hmm", "hm", "mm", "mmhmm", "ok",
    "okay", "okey", "acha", "accha", "achha", "theek", "thik", "sahi", "right",
    "sure", "hello", "helo", "hi", "alright", "fine", "correct", "bilkul", "chalo",
    "toh", "to", "and", "so",
}
BACKCHANNEL_STRIP = " .,!?।-–—…\"'\u2018\u2019\u201c\u201d"


def is_backchannel(text):
    """True when the turn is nothing but acknowledgement tokens.

    Empty and punctuation-only turns count too: there is nothing in them to judge.
    A turn of four or more words is never treated as backchannel however ordinary
    the words are, because at that length they are a sentence doing something.
    """
    words = [word.strip(BACKCHANNEL_STRIP).lower()
             for word in (text or "").replace("\u0964", " ").split()]
    words = [word for word in words if word]
    if not words:
        return True
    if len(words) > 3:
        return False
    return all(word in BACKCHANNEL_WORDS for word in words)


def scan_turn(api_key, model, turns, target_index, context_turns, timeout,
              reasoning_effort):
    """Review one Employee turn in context. Returns a flag dict, or None."""
    window = render_window(turns, target_index, context_turns)
    try:
        result, usage = post_chat(
            api_key, model,
            [{"role": "system", "content": CONDUCT_SYSTEM_PROMPT},
             {"role": "user", "content": window}],
            CONDUCT_SCHEMA, "conduct_check", timeout, reasoning_effort,
            cache_key=CONDUCT_CACHE_KEY,
        )
    except RuntimeError as exc:
        # One failed window must not lose the other thirty-five.
        print("  ! turn %d skipped: %s" % (target_index + 1, exc), file=sys.stderr)
        return None, {}

    if not result.get("flagged"):
        return None, usage

    speaker, text = turns[target_index]
    return {
        "turn": target_index + 1,
        "speaker": speaker,
        "phrase": (result.get("phrase") or text).strip(),
        "category": result.get("category") or "other",
        "severity": result.get("severity") or "medium",
        "reason": result.get("reason") or "",
        "source": "conduct_scan",
    }, usage


def run_conduct_scan(api_key, model, turns, context_turns, timeout, reasoning_effort,
                     workers):
    """Review every Employee turn against the last `context_turns` messages.

    Returns (flags sorted by turn, a cost.UsageLedger). Windows are independent, so
    they run concurrently; ordering is restored from the turn index afterwards.

    The ledger replaces the hand-rolled sum that used to live here. That sum kept
    only the top-level integers, which threw away `prompt_tokens_details` — so the
    cached share of the prompt, which on this pass is most of it, never reached the
    output file and the scan looked far more expensive than it was.
    """
    employee_turns = [i for i, (speaker, _) in enumerate(turns) if is_employee(speaker)]
    targets = [i for i in employee_turns if not is_backchannel(turns[i][1])]
    skipped = len(employee_turns) - len(targets)
    ledger = cost.UsageLedger(model, "analyze.conduct_scan")
    if not targets:
        return [], ledger

    print("Conduct scan: %d employee turns (%d pure acknowledgements skipped), "
          "%d turns of context, model=%s, reasoning=%s ..."
          % (len(employee_turns), skipped, context_turns, model, reasoning_effort),
          file=sys.stderr)

    flags = []

    def collect(result):
        flag, usage = result
        ledger.record(usage)
        if flag:
            flags.append(flag)

    # The first window is sent on its own before the rest fan out. Every request on
    # this pass repeats the same long system prompt, and the provider only serves it
    # from cache once some request has written it there — so starting four workers at
    # once means four of them pay to write the same block. One request first, then
    # the pool, and the other thirty read what it wrote.
    collect(scan_turn(api_key, model, turns, targets[0], context_turns,
                      timeout, reasoning_effort))

    if len(targets) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(scan_turn, api_key, model, turns, index, context_turns,
                            timeout, reasoning_effort)
                for index in targets[1:]
            ]
            # Recorded here rather than inside the worker: one ledger touched by one
            # thread needs no lock.
            for future in concurrent.futures.as_completed(futures):
                collect(future.result())

    flags.sort(key=lambda entry: entry["turn"])
    print("  " + ledger.summary_line(), file=sys.stderr)
    return flags, ledger


SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def merge_neighbouring_flags(flags):
    """Collapse a run of flagged turns from one escalation into a single finding.

    The same moment is reviewed by the scan and by the whole-call pass and is reported
    twice or three times, so one lapse arrives looking like several. Flags of the same
    category close together become one finding anchored at the earliest turn, with the
    rest kept in `also_at_turns` so nothing is silently dropped.

    The window is measured **from the anchor**, not from the last turn folded into it.
    Chaining off the last turn let one finding walk the length of an argument — 24
    absorbs 26, 26 absorbs 30, 30 absorbs 34 — until a ten-minute row was a single
    flag and the worst thing said in it was a turn number inside somebody else's
    entry. A moment is a moment: past `MERGE_GAP_TURNS` from where it started, it is a
    second one, and a counsellor who was patronising at 24 and then told the customer
    to mind their manners at 30 did two different things.
    """
    merged = []
    for flag in sorted(flags, key=lambda entry: entry.get("turn") or 0):
        previous = merged[-1] if merged else None
        if (previous
                and flag.get("category") == previous.get("category")
                and (flag.get("turn") or 0) - (previous.get("turn") or 0)
                <= MERGE_GAP_TURNS):
            previous.setdefault("also_at_turns", []).append(flag.get("turn"))
            if SEVERITY_ORDER.get(flag.get("severity"), 1) > SEVERITY_ORDER.get(
                    previous.get("severity"), 1):
                previous["severity"] = flag.get("severity")
            # A moment either pass saw stays attributed to the whole-call pass, so a
            # reviewer reads it as a finding the full-call review stood behind.
            if flag.get("source") != "conduct_scan":
                previous["source"] = flag.get("source", "whole_call")
            continue
        merged.append(dict(flag))

    # A turn that anchors its own finding never stays inside another one's spread.
    # The whole-call pass sometimes sweeps a later, different lapse into `also_at_turns`
    # on an earlier entry; left there it would be swallowed downstream, where a turn
    # already accounted for is skipped rather than raised.
    anchors = {entry.get("turn") for entry in merged}
    for entry in merged:
        spread = sorted({turn for turn in entry.get("also_at_turns", [])
                         if turn is not None and turn != entry.get("turn")
                         and turn not in anchors})
        if spread:
            entry["also_at_turns"] = spread
            entry["reason"] = ("%s Continues across turns %s."
                               % (str(entry.get("reason", "")).rstrip(),
                                  ", ".join(str(turn) for turn in spread)))
    return merged


def add_scan_flags(analysis, scan_flags):
    """Fold scan findings into the existing flagged_phrases list.

    Same array, same item shape — everything downstream (audit_call.py's
    reconcile_conduct_flags, the report UI) keeps working untouched, and the scan's
    findings are carried into the audit output exactly like the whole-call pass's.
    The two passes are merged together rather than concatenated, so a stretch both
    of them saw is reported once. Only runs when the scan ran: with `--no-scan` the
    whole-call output is left exactly as the model returned it.
    """
    existing = [entry for entry in (analysis.get("flagged_phrases") or [])
                if isinstance(entry, dict)]
    if not scan_flags:
        return 0

    merged = merge_neighbouring_flags(existing + list(scan_flags))
    analysis["flagged_phrases"] = merged
    analysis["inappropriate_language_found"] = bool(merged)
    return max(0, len(merged) - len(existing))


# --------------------------------------------------------------------------- #
# Input / config
# --------------------------------------------------------------------------- #
def load_api_key(env_path):
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key.strip()

    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == "OPENAI_API_KEY":
                    return value.strip().strip("'\"")

    sys.exit("OPENAI_API_KEY not found. Set it in the environment or in %s" % env_path)


def load_turns(path):
    """Accept the clean format ([{Role: text}]) or the detailed transcribe.py format."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (IOError, OSError) as exc:
        sys.exit("Could not read %s: %s" % (path, exc))
    except ValueError as exc:
        sys.exit("%s is not valid JSON: %s" % (path, exc))

    if isinstance(data, dict):
        data = data.get("transcript", [])

    turns = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if "speaker" in entry:  # detailed format from transcribe.py
            speaker = entry.get("speaker", "Unknown")
            text = entry.get("text", entry.get("statement", ""))
            if str(text).strip():
                turns.append((str(speaker), str(text).strip()))
        else:  # clean format: one key per turn
            for speaker, text in entry.items():
                if str(text).strip():
                    turns.append((str(speaker), str(text).strip()))

    if not turns:
        sys.exit("No usable turns found in %s" % path)
    return turns


def render_transcript(turns):
    return "\n".join(
        "%d. %s: %s" % (index, speaker, text)
        for index, (speaker, text) in enumerate(turns, start=1)
    )


# --------------------------------------------------------------------------- #
# LLM call
# --------------------------------------------------------------------------- #
def post_chat(api_key, model, messages, schema, schema_name, timeout,
              reasoning_effort=None, cache_key=None):
    """POST one chat completion with a strict JSON schema. Raises RuntimeError.

    `reasoning_effort` is sent when given; a model that does not accept the
    parameter is retried once without it rather than failing the run.

    `cache_key` asks the provider to route requests that share it to the same
    cache. Every request on a pass sends the same multi-thousand-token system
    prompt, and without the key the concurrent workers each land somewhere
    different and each pay to write their own copy of it.
    """
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if cache_key:
        payload["prompt_cache_key"] = cache_key

    def send(body):
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": "Bearer %s" % api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    # Two of the parameters above are optional extras rather than requirements, and
    # a model that has never heard of one rejects the whole request over it. Each is
    # dropped once, in turn, rather than failing a run over a parameter that was only
    # ever an optimisation.
    optional_params = ("reasoning_effort", "prompt_cache_key")
    try:
        result = send(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        result = None
        for name in optional_params:
            if name not in detail or name not in payload:
                continue
            payload.pop(name, None)
            try:
                result = send(payload)
                break
            except urllib.error.HTTPError as retry_exc:
                detail = retry_exc.read().decode("utf-8", "replace")
                exc = retry_exc
        if result is None:
            raise RuntimeError("OpenAI API error %s: %s" % (exc.code, detail[:500]))
    except urllib.error.URLError as exc:
        raise RuntimeError("Could not reach the OpenAI API (%s)" % exc.reason)

    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise RuntimeError("Unexpected OpenAI response: %s" % json.dumps(result)[:500])

    if not content:
        raise RuntimeError("Model returned empty content (finish_reason=%s)"
                           % result["choices"][0].get("finish_reason"))

    try:
        return json.loads(content), result.get("usage", {})
    except ValueError:
        raise RuntimeError("Model did not return valid JSON: %s" % content[:500])


def call_openai(api_key, model, transcript_text, timeout, reasoning_effort=None):
    """The whole-call pass. Unchanged in what it asks for; a failure here is fatal."""
    try:
        return post_chat(
            api_key, model,
            [{"role": "system", "content": SYSTEM_PROMPT},
             {"role": "user",
              "content": "Analyse this sales call transcript.\n\n"
                         "TRANSCRIPT (turn number. speaker: statement)\n"
                         "%s" % transcript_text}],
            RESPONSE_SCHEMA, "call_analysis", timeout, reasoning_effort,
            cache_key=WHOLE_CALL_CACHE_KEY,
        )
    except RuntimeError as exc:
        sys.exit(str(exc))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_report(analysis):
    print("Overall sentiment : %s (score %s)"
          % (analysis.get("overall_sentiment"), analysis.get("sentiment_score")))
    print("Customer interest : %s" % analysis.get("customer_interest_level"))
    print("\nSummary\n  %s" % analysis.get("call_tone_summary", ""))

    employee = analysis.get("employee_tone") or {}
    customer = analysis.get("customer_tone") or {}
    print("\nEmployee tone : %s (politeness %s)"
          % (", ".join(employee.get("labels", [])), employee.get("politeness_score")))
    print("  %s" % employee.get("description", ""))
    print("Customer tone : %s" % ", ".join(customer.get("labels", [])))
    print("  %s" % customer.get("description", ""))
    print("\nProgression: %s" % analysis.get("tone_progression", ""))

    flags = analysis.get("flagged_phrases") or []
    print("\nInappropriate language: %s" % ("YES — %d flag(s)" % len(flags)
                                            if flags else "none detected"))
    for flag in flags:
        print("  [turn %s] %s (%s, %s)%s"
              % (flag.get("turn"), flag.get("speaker"),
                 flag.get("category"), flag.get("severity"),
                 "  <- conduct scan" if flag.get("source") == "conduct_scan" else ""))
        print("      phrase: %s" % flag.get("phrase"))
        print("      reason: %s" % flag.get("reason"))

    if analysis.get("notes"):
        print("\nNotes: %s" % analysis["notes"])


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sentiment and inappropriate-language analysis of a sales call."
    )
    parser.add_argument("transcript",
                        help="Path to the cleaned transcript JSON")
    parser.add_argument("-o", "--output",
                        help="Output path (default: <input basename>.analysis.json)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="OpenAI model (default: %s)" % DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=180,
                        help="Request timeout in seconds (default: 180)")
    parser.add_argument("--print", dest="print_report", action="store_true",
                        help="Print a readable report to stdout")
    parser.add_argument("--context", type=int, default=DEFAULT_CONTEXT_TURNS,
                        help="Turns of context behind each Employee turn in the "
                             "conduct scan (default: %d)" % DEFAULT_CONTEXT_TURNS)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                        choices=["minimal", "low", "medium", "high"],
                        help="Reasoning effort for both passes (default: %s)"
                             % DEFAULT_REASONING_EFFORT)
    parser.add_argument("--scan-model", default=DEFAULT_SCAN_MODEL,
                        help="Model for the per-turn conduct scan, which makes most "
                             "of the requests and most of the cost (default: %s)"
                             % DEFAULT_SCAN_MODEL)
    parser.add_argument("--scan-workers", type=int, default=DEFAULT_SCAN_WORKERS,
                        help="Concurrent conduct-scan requests (default: %d)"
                             % DEFAULT_SCAN_WORKERS)
    parser.add_argument("--no-scan", dest="scan", action="store_false",
                        help="Skip the per-turn conduct scan (whole-call pass only)")
    return parser.parse_args()


def main():
    args = parse_args()

    input_path = os.path.abspath(args.transcript)
    if not os.path.isfile(input_path):
        sys.exit("Transcript file not found: %s" % input_path)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    api_key = load_api_key(os.path.join(script_dir, ".env"))

    base = os.path.splitext(input_path)[0]
    for suffix in (".clean", ".transcript"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    output_path = args.output or (base + ".analysis.json")

    turns = load_turns(input_path)
    transcript_text = render_transcript(turns)

    print("Analysing %d turns with %s ..." % (len(turns), args.model), file=sys.stderr)
    analysis, usage = call_openai(api_key, args.model, transcript_text, args.timeout,
                                  args.reasoning_effort)
    whole_call = cost.UsageLedger(args.model, "analyze.whole_call").record(usage)
    print("  " + whole_call.summary_line(), file=sys.stderr)

    scan_ledger = cost.UsageLedger(args.scan_model, "analyze.conduct_scan")
    scan_raw = []
    scan_added = 0
    if args.scan:
        scan_flags, scan_ledger = run_conduct_scan(
            api_key, args.scan_model, turns, args.context, args.timeout,
            args.reasoning_effort, args.scan_workers,
        )
        scan_raw = scan_flags
        scan_added = add_scan_flags(analysis, scan_flags)
        print("Conduct scan: %d flag(s) added (%d raw)"
              % (scan_added, len(scan_flags)), file=sys.stderr)

    document = {
        "source_transcript": os.path.basename(input_path),
        "model": args.model,
        "turns_analysed": len(turns),
        "conduct_scan": {
            "ran": bool(args.scan),
            "model": args.scan_model,
            "context_turns": args.context,
            "reasoning_effort": args.reasoning_effort,
            "flags_added": scan_added,
            "raw_flags": scan_raw,
        },
        # Raw provider blocks are kept as the evidence behind `cost`; the dollars
        # are stored alongside because a rate card that moves later must not
        # silently restate what this run actually cost.
        "token_usage": usage,
        "scan_token_usage": scan_ledger.as_dict(),
        "cost": {
            "whole_call": whole_call.as_dict(),
            "conduct_scan": scan_ledger.as_dict(),
            "total_usd": round(cost.combine(whole_call, scan_ledger) or 0.0, 6),
        },
        "analysis": analysis,
    }

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=2)
    stage_total = cost.combine(whole_call, scan_ledger)
    print("Analysis written to %s (stage cost $%.4f)"
          % (output_path, stage_total or 0.0), file=sys.stderr)

    if args.print_report:
        print_report(analysis)


if __name__ == "__main__":
    main()

---
name: livenote-elder-friendly-summary
description: Create plain-language, elder-friendly summaries of Chinese livestream health Q&A, preserving who said what while separating practitioner advice, public anecdotes, and unverified folk remedies. Use when summarizing LiveNote transcripts or preparing knowledge-card content from them.
---

# LiveNote Elder-Friendly Summary

Use this skill when a LiveNote transcript comes from a Chinese livestream in which a folk practitioner answers callers' health questions and callers may share personal remedies or home treatments.

## Intended result

Produce a short, readable summary for ordinary older adults. Keep the wording close to everyday spoken Chinese, remove repetition, and preserve the meaning of the original exchange. The output is an organized record of what people said, not a medical diagnosis or a recommendation to try a remedy.

## Non-negotiable safety and attribution rules

- Keep the speaker attached to every medical claim. Distinguish clearly between the practitioner, the caller, and another person mentioned in the exchange.
- Separate these categories instead of blending them: the caller's question or symptoms; the practitioner's answer; a remedy or experience offered by a caller; and an uncertain or incomplete statement.
- Never turn a folk remedy, personal experience, or practitioner opinion into a proven treatment. Use wording such as “有人提到……”“来电者说自己试过……”“大夫建议……”，only when the source actually supports it.
- Do not invent a diagnosis, effectiveness, dosage, contraindication, cause, or medical conclusion. Do not silently correct the source with outside medical knowledge.
- If the source includes advice to stop prescribed medicine, delay urgent care, use a risky substance, or treat a serious symptom at home, preserve the attribution and mark it as “需要正规医疗人员确认” or “存在风险，不能仅凭这段内容处理”. Do not present it as an action item to follow.
- If a sentence is unclear, say “原话不清楚，无法确认”，rather than guessing.
- Keep uncertainty visible. “没有说明”“只是个人经验”“还不能确定” are valid outcomes.

## Language and audience

- Write for an older reader in China. Prefer common words, short sentences, and concrete descriptions.
- Avoid academic, bureaucratic, promotional, or technical wording. Explain an unavoidable medical term in plain Chinese immediately.
- Do not use the words “录音”“音频”“转写”“识别”“模型”“AI”“Codex” or “系统” in the user-facing summary. Describe the subject directly.
- Do not write “这段录音讲了……” or describe how the text was produced. Start with the matter itself.
- Avoid generic AI phrasing, empty conclusions, excessive headings, long lead-ins, and forced three-part lists. Do not add humor or dramatic language that could make health claims sound reliable.
- Keep the tone calm and respectful. Do not ridicule folk beliefs or callers, but do not use politeness to hide uncertainty or risk.

## Output contract

Keep the existing LiveNote result shape so the product can store and display it:

- `title`: a short, concrete title; do not mention the source format.
- `overview`: one or two plain-language sentences answering what this exchange was mainly about.
- `knowledgeStructure`: use simple section titles such as “问了什么”“大夫怎么说”“有人分享的办法”. Put each claim under the correct speaker or source.
- `keyPoints`: the few points an older reader should remember. Include attribution when a point could be mistaken for medical fact.
- `questions`: preserve the caller's question and the answer when both are clear. Do not invent an answer.
- `actionItems`: only safe, source-supported next steps. Do not convert an unverified remedy into an instruction.
- `confidenceNotes`: record unclear words, missing context, conflicting statements, and medical-risk cautions. Omit generic “请复核” filler.

Prefer fewer, useful points over a comprehensive academic outline. If the exchange contains several callers, keep each caller's question and answer together instead of merging them by medical topic.

## Long sessions and multiple callers

Sessions may last about 120 minutes and can include many callers. Clarity is more important than making one short paragraph.

- Keep the original order of callers and topics unless a small reordering makes the result much easier to understand.
- Start with a short overall overview, then divide the body into clearly labeled caller or question sections. Use labels such as “第一位来电者”“第二位来电者” when no name is clearly available. Never invent names or identities.
- Each caller section should follow the same readable order when the information exists: “问了什么” → “大夫怎么说” → “对方分享的办法” → “需要注意”. Do not add empty headings.
- Keep a caller's symptoms, question, answer, remedy, and risk note together. Do not merge two callers just because they mentioned the same illness or remedy.
- If a caller interrupts, changes topic, or the speaker cannot be identified, state that briefly instead of assigning the statement to the wrong person.
- For repeated topics, summarize the common point only after the individual caller sections, and keep any disagreement or different advice visible.
- Put the per-caller structure in `knowledgeStructure`; use `questions` for each clear question-and-answer pair. Keep `keyPoints` short and grouped or prefixed so a reader can tell which caller a point belongs to.
- Never let the length of the session justify a single sweeping conclusion. A long result may be longer, but it must remain scannable and easy to return to later.

## Final review

Before returning the result, check:

1. Could an older reader understand it without knowing LiveNote or Codex?
2. Is every health-related claim attributed to the right speaker?
3. Did any personal remedy become a treatment recommendation by accident?
4. Did the summary preserve uncertainty and risk instead of guessing?
5. Are the forbidden technical source words absent from user-facing text?

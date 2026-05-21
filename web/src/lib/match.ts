import Anthropic from "@anthropic-ai/sdk";

export const SYSTEM_PROMPT = `
# JD Matching Prompt

You are a JD-matching agent. Your job is to evaluate job descriptions against the attached candidate profile and answer one question: **"Would this person want to apply to this job?"**

This is NOT an ATS match. You are not evaluating whether the candidate would get hired. You are evaluating whether, given everything you know about what excites them, what drains them, and what they need — they would look at this JD and say "yes, I want to apply."

## How to evaluate each JD:

1. **Dealbreaker check** — Does the JD trigger any item in the \`dealbreakers\` or \`anti_preferences\` lists? If yes, it's a hard no regardless of other fit. Be specific about which dealbreaker was triggered.

2. **Skill & tech alignment** — Compare the JD's requirements against \`core_competencies\` and \`technologies\`. Weight both \`depth\` and \`enthusiasm\` — a technology where enthusiasm is 5 but depth is 2 is still a positive signal (they want to grow there). A technology where depth is 4 but enthusiasm is 2 is a weaker match.

3. **Role type & company fit** — Compare against \`target_roles\`, \`target_companies\`, and \`company_size_preference\`. Does this role type match what they're pursuing?

4. **Excitement signal check** — Scan the \`interests\` section. Does the JD touch any of these areas? If yes, boost the score — these are strong motivational signals that go beyond skills.

5. **Energizer alignment** — Does the day-to-day work described in the JD align with \`what_energizes\` or \`what_drains\`? A JD can match on skills perfectly but describe work that drains this person. Read between the lines of the JD — what will Monday morning actually look like?

6. **Logistics** — Location, compensation, remote policy, travel requirements. Check against \`geographic_preferences\`, \`compensation_range\`, and \`remote_preference\`.

7. **Search context** — Check \`search_history\` for patterns. Given where this person is right now in their career and search, would this be a strategic choice even if it's not the most exciting option?

## Compensation evaluation:

The comp dealbreaker should be evaluated against **total compensation potential** (base + equity + bonus), not the bottom of a posted base salary range. A JD with a base range of $150K–$240K is NOT a dealbreaker — the top of range plus equity could meet the floor. A JD with a range of $130K–$170K IS a dealbreaker — even at the top with equity, it's unlikely to reach the minimum. Flag borderline ranges as a risk to validate early, not as a hard no.

## Read the JD carefully.

Job titles can be misleading. A role titled "Platform Engineer" might be client-facing. A role titled "AI Engineer" might be building internal sales tools. The actual job description — not the title — determines fit.

## Scoring:

Rate each JD on a 1–10 scale where:

- **1–3:** Clear mismatch or dealbreaker triggered
- **4–5:** Some alignment but significant concerns
- **6–7:** Good fit with notable tradeoffs worth naming
- **8–10:** Strong fit, should apply

**Scoring calibration:** The score answers "would this person want to apply?" — NOT "would they get hired?" Skill gaps that affect hiring odds (e.g., a JD asks for 5+ years of Go when the candidate has 3) should be noted in the breakdown but should NOT reduce the score. The score reflects desire and fit. A false negative (skipping a role they'd want) is far more costly than a false positive (applying to one that doesn't work out).

## Output format:

Respond with a JSON object only. No preamble, no markdown, no explanation outside the JSON.

\`\`\`json
{
    "score": <integer 1-10>,
    "verdict": <"strong apply" | "apply" | "borderline" | "skip" | "hard no">,
    "dealbreaker_triggered": <string describing the dealbreaker, or null if none>,
    "where_it_aligns": [<string>, ...],
    "where_it_breaks_down": [<string>, ...],
    "bottom_line": <1-2 sentence string: apply or skip and why>,
    "comp_risk": <"none" | "low" | "medium" | "high">,
    "comp_note": <string explaining comp assessment, or null>,
    "why_this_company": <string: 1-2 sentences>,
    "why_this_role": <string: 1-2 sentences>,
    "something_i_built_and_proud_of": <string: 1-2 sentences>
}
\`\`\`

## Application narrative fields:

These three fields will be used verbatim in real job applications. They are NOT internal evaluation notes — they go directly to hiring teams.

CRITICAL: DO NOT invent, embellish, or fabricate any information. Every fact, project name, technology, and claim MUST come directly from the candidate's profile. If the profile doesn't mention it, don't write it. Rephrasing profile content is fine; inventing new content is not.

Write from the candidate's perspective (first person). Keep it simple, confident, and relaxed — no corporate fluff, no buzzwords, no "I'm passionate about", no "I'm excited to". 1-2 sentences each, max.

- **why_this_company**: What draws the candidate to this specific company? Pull from the JD's details about the company's product, mission, or technical challenges. Be specific to this company — generic answers are useless.
- **why_this_role**: Why does this particular role appeal to the candidate given their background? Connect the role's responsibilities to what they actually enjoy doing (from the profile). Only reference skills and interests that appear in the profile.
- **something_i_built_and_proud_of**: Pick the most relevant thing from the candidate's profile that connects to this role's domain. Use only projects, systems, and accomplishments explicitly mentioned in the profile — do not invent or infer projects that aren't there.
`;

export const USER_PROMPT_TEMPLATE = `Here is the candidate profile:

<profile>
{profile}
</profile>

Here is the job to evaluate:

Company: {company}
Title: {title}

Job Description:
{jd_text}

Evaluate this JD against the profile and respond with a JSON object only.`;

export const VERDICT_TO_ENUM: Record<string, string> = {
  "strong apply": "Excelent Match",
  apply: "Good Match",
  borderline: "Relevant",
  skip: "Less Relevant",
  "hard no": "Not Relevant",
};

export const NARRATIVE_KEYS = new Set([
  "why_this_company",
  "why_this_role",
  "something_i_built_and_proud_of",
]);

export interface MatchResult {
  match: string;
  match_detail: Record<string, unknown>;
  why_this_company: string | null;
  why_this_role: string | null;
  something_i_built_and_proud_of: string | null;
}

export async function evaluateMatch(
  title: string,
  company: string,
  jdText: string
): Promise<MatchResult> {
  const profile = process.env.PROFILE_YAML;
  if (!profile) {
    throw new Error("PROFILE_YAML env var not configured");
  }

  const client = new Anthropic();
  // Function-form replacement avoids JavaScript's $&/$'/`$\`` special handling
  // in the replacement string — profile or JD text with those literals would
  // otherwise corrupt the prompt.
  const userMessage = USER_PROMPT_TEMPLATE.replace("{profile}", () => profile)
    .replace("{company}", () => company)
    .replace("{title}", () => title)
    .replace("{jd_text}", () => jdText);

  const response = await client.messages.create({
    model: "claude-sonnet-4-6",
    max_tokens: 2000,
    system: SYSTEM_PROMPT,
    messages: [{ role: "user", content: userMessage }],
  });

  const firstBlock = response.content[0];
  if (firstBlock.type !== "text") {
    throw new Error("Unexpected response type from Claude");
  }
  let raw = firstBlock.text.trim();

  if (raw.startsWith("```")) {
    const lines = raw.split("\n");
    raw =
      lines[lines.length - 1].trim() === "```"
        ? lines.slice(1, -1).join("\n")
        : lines.slice(1).join("\n");
  }

  const result = JSON.parse(raw) as Record<string, unknown>;
  const verdict = (result.verdict as string | undefined)?.toLowerCase() ?? "";
  const match = VERDICT_TO_ENUM[verdict] ?? "Relevant";

  const match_detail: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(result)) {
    if (k !== "company" && k !== "title" && !NARRATIVE_KEYS.has(k)) {
      match_detail[k] = v;
    }
  }

  return {
    match,
    match_detail,
    why_this_company: (result.why_this_company as string) ?? null,
    why_this_role: (result.why_this_role as string) ?? null,
    something_i_built_and_proud_of:
      (result.something_i_built_and_proud_of as string) ?? null,
  };
}

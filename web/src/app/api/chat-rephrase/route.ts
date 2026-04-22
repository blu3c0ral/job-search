import { NextRequest, NextResponse } from "next/server";
import Anthropic from "@anthropic-ai/sdk";
import { readFileSync } from "fs";
import { getSupabase } from "@/lib/supabase";

const MODELS: Record<string, string> = {
  opus: "claude-opus-4-6",
  sonnet: "claude-sonnet-4-6",
};

export async function POST(request: NextRequest) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return NextResponse.json(
      { error: "ANTHROPIC_API_KEY not configured" },
      { status: 500 }
    );
  }

  const { id, source_platform, field, userMessage, model, resumeText } =
    await request.json();

  if (!id || !source_platform || !field || !userMessage || !model) {
    return NextResponse.json(
      { error: "Missing required fields" },
      { status: 400 }
    );
  }

  const modelId = MODELS[model];
  if (!modelId) {
    return NextResponse.json({ error: "Invalid model" }, { status: 400 });
  }

  // Fetch job data
  const supabase = getSupabase();
  const { data: job, error } = await supabase
    .from("job_search_main")
    .select("*")
    .eq("id", id)
    .eq("source_platform", source_platform)
    .single();

  if (error || !job) {
    return NextResponse.json(
      { error: "Job not found" },
      { status: 404 }
    );
  }

  // Load profile
  let profile = "";
  const profilePath = process.env.PROFILE_YAML_PATH;
  if (profilePath) {
    try {
      profile = readFileSync(profilePath, "utf-8");
    } catch {
      profile = "(profile not available)";
    }
  }

  const fieldLabels: Record<string, string> = {
    why_this_company: "Why this company?",
    why_this_role: "Why this role?",
    something_i_built_and_proud_of: "Something I built and I'm proud of",
  };

  const currentAnswer = job[field] ?? "(empty)";

  const systemPrompt = `You are a career coach helping a job applicant rephrase and improve their application narrative answers.

You have access to all the context about this job and the candidate. Use it to provide a rephrased version that is:
- Authentic and grounded in the candidate's actual experience (from the profile)
- Specific to the company and role (from the JD and match details)
- Concise (1-3 sentences, matching the original style)
- Natural and conversational, not corporate-speak

Here is all the context:

## Candidate Profile
${profile}

## Job Details
- Company: ${job.company}
- Role: ${job.role_title}
- Location: ${job.location}
- Platform: ${job.source_platform}

## Job Description
${job.job_description ?? "(not available)"}

## Match Analysis
${job.match_detail ? JSON.stringify(job.match_detail, null, 2) : "(not available)"}

## Current Narrative Answers
- Why this company: ${job.why_this_company ?? "(empty)"}
- Why this role: ${job.why_this_role ?? "(empty)"}
- Something I built and I'm proud of: ${job.something_i_built_and_proud_of ?? "(empty)"}

## Tailored Resume (full text)
${resumeText || "(no tailored resume available)"}

## Resume Tailoring Changes
${job.tailoring_changes ? JSON.stringify(job.tailoring_changes, null, 2) : "(not available)"}

---

The user is asking for help with the field: **${fieldLabels[field] ?? field}**
Current answer: "${currentAnswer}"

Provide a rephrased version based on the user's request. Return ONLY the rephrased text — no explanations, no quotes, no labels. Just the answer they can copy-paste.`;

  console.log("\n=== REPHRASE CHAT REQUEST ===");
  console.log(`Job: ${job.company} - ${job.role_title} (${source_platform}/${id})`);
  console.log(`Field: ${fieldLabels[field] ?? field}`);
  console.log(`Current answer: ${currentAnswer}`);
  console.log(`Model: ${modelId}`);
  console.log(`User message: ${userMessage}`);
  console.log(`Profile loaded: ${profile ? `${profile.length} chars` : "no"}`);
  console.log(`Resume text: ${resumeText ? `${resumeText.length} chars` : "none"}`);
  console.log(`System prompt: ${systemPrompt.length} chars`);
  console.log("--- FULL SYSTEM PROMPT ---");
  console.log(systemPrompt);
  console.log("--- END SYSTEM PROMPT ---");

  const client = new Anthropic({ apiKey });
  const start = Date.now();

  const response = await client.messages.create({
    model: modelId,
    max_tokens: 1024,
    system: [{ type: "text", text: systemPrompt, cache_control: { type: "ephemeral" } }],
    messages: [{ role: "user", content: userMessage }],
  });

  const elapsed = Date.now() - start;
  const text =
    response.content[0].type === "text" ? response.content[0].text : "";

  console.log(`\n--- RESPONSE (${elapsed}ms) ---`);
  console.log(`Model: ${response.model}`);
  console.log(`Input tokens: ${response.usage.input_tokens} (cache read: ${(response.usage as Record<string, number>).cache_read_input_tokens ?? 0}, cache creation: ${(response.usage as Record<string, number>).cache_creation_input_tokens ?? 0})`);
  console.log(`Output tokens: ${response.usage.output_tokens}`);
  console.log(`Stop reason: ${response.stop_reason}`);
  console.log(`Response: ${text}`);
  console.log("=== END REPHRASE CHAT ===\n");

  return NextResponse.json({ response: text });
}

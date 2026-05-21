import { NextRequest, NextResponse } from "next/server";
import { getSupabase } from "@/lib/supabase";
import { evaluateMatch } from "@/lib/match";
import { randomUUID } from "crypto";

export async function POST(request: NextRequest) {
  const body = await request.json();
  const {
    company,
    role_title,
    job_description,
    location,
    compensation,
    link,
    apply_url,
    status,
    date_found,
    applied_date,
  } = body as Record<string, string | undefined>;

  if (!company || !role_title || !job_description) {
    return NextResponse.json(
      { error: "company, role_title, and job_description are required" },
      { status: 400 }
    );
  }

  const id = randomUUID();
  const source_platform = "manual";

  let matchResult: Awaited<ReturnType<typeof evaluateMatch>>;
  try {
    matchResult = await evaluateMatch(role_title, company, job_description);
  } catch (err) {
    return NextResponse.json(
      {
        error: `Match evaluation failed: ${err instanceof Error ? err.message : String(err)}`,
      },
      { status: 500 }
    );
  }

  const row: Record<string, unknown> = {
    id,
    source_platform,
    company,
    role_title,
    job_description,
    location: location || null,
    compensation: compensation || null,
    link: link || null,
    apply_url: apply_url || link || null,
    status: status || "New",
    date_found: date_found || new Date().toISOString().split("T")[0],
    search_term_match: "manual",
    match: matchResult.match,
    match_detail: matchResult.match_detail,
    why_this_company: matchResult.why_this_company,
    why_this_role: matchResult.why_this_role,
    something_i_built_and_proud_of: matchResult.something_i_built_and_proud_of,
  };

  if (applied_date) {
    row.applied_date = applied_date;
  }

  const supabase = getSupabase();
  const { error } = await supabase.from("job_search_main").insert(row);

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ platform: source_platform, id });
}

import { NextRequest, NextResponse } from "next/server";
import { getSupabase } from "@/lib/supabase";
import { evaluateMatch } from "@/lib/match";

export async function POST(request: NextRequest) {
  const body = await request.json();
  const {
    id,
    source_platform,
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
    rerun_match,
  } = body as Record<string, string | boolean | undefined>;

  if (!id || !source_platform) {
    return NextResponse.json(
      { error: "id and source_platform are required" },
      { status: 400 }
    );
  }
  if (!company || !role_title || !job_description) {
    return NextResponse.json(
      { error: "company, role_title, and job_description are required" },
      { status: 400 }
    );
  }

  const updates: Record<string, unknown> = {
    company,
    role_title,
    job_description,
    location: (location as string) || null,
    compensation: (compensation as string) || null,
    link: (link as string) || null,
    apply_url: (apply_url as string) || (link as string) || null,
    status: status || "New",
    date_found,
  };

  if (applied_date) {
    updates.applied_date = applied_date;
  } else if (
    typeof applied_date === "string" &&
    applied_date === ""
  ) {
    updates.applied_date = null;
  }

  if (rerun_match) {
    try {
      const matchResult = await evaluateMatch(
        role_title as string,
        company as string,
        job_description as string
      );
      updates.match = matchResult.match;
      updates.match_detail = matchResult.match_detail;
      updates.why_this_company = matchResult.why_this_company;
      updates.why_this_role = matchResult.why_this_role;
      updates.something_i_built_and_proud_of =
        matchResult.something_i_built_and_proud_of;
    } catch (err) {
      return NextResponse.json(
        {
          error: `Match re-evaluation failed: ${err instanceof Error ? err.message : String(err)}`,
        },
        { status: 500 }
      );
    }
  }

  const supabase = getSupabase();
  const { error } = await supabase
    .from("job_search_main")
    .update(updates)
    .eq("id", id)
    .eq("source_platform", source_platform);

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ ok: true });
}

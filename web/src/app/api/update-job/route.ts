import { NextRequest, NextResponse } from "next/server";
import { getSupabase } from "@/lib/supabase";

export async function POST(request: NextRequest) {
  const body = await request.json();
  const { id, source_platform, ...updates } = body;

  if (!id || !source_platform) {
    return NextResponse.json(
      { error: "Missing id or source_platform" },
      { status: 400 }
    );
  }

  // Only allow updating specific fields
  const allowed = [
    "status",
    "why_this_company",
    "why_this_role",
    "something_i_built_and_proud_of",
  ];
  const filtered: Record<string, string> = {};
  for (const key of allowed) {
    if (key in updates) {
      filtered[key] = updates[key];
    }
  }

  if (Object.keys(filtered).length === 0) {
    return NextResponse.json({ error: "No valid fields to update" }, { status: 400 });
  }

  const supabase = getSupabase();
  const { error } = await supabase
    .from("job_search_main")
    .update(filtered)
    .eq("id", id)
    .eq("source_platform", source_platform);

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ ok: true });
}

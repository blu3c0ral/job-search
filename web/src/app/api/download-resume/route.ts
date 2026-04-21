import { NextRequest, NextResponse } from "next/server";
import { getSupabase } from "@/lib/supabase";

export async function GET(request: NextRequest) {
  const path = request.nextUrl.searchParams.get("path");
  if (!path) {
    return NextResponse.json({ error: "Missing path" }, { status: 400 });
  }

  const supabase = getSupabase();
  const { data, error } = await supabase.storage
    .from("tailored-resumes")
    .createSignedUrl(path, 60, { download: "resume.docx" });

  if (error || !data?.signedUrl) {
    return NextResponse.json(
      { error: error?.message ?? "Failed to create signed URL" },
      { status: 500 }
    );
  }

  return NextResponse.redirect(data.signedUrl);
}

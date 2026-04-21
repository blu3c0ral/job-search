import { getSupabase } from "@/lib/supabase";
import { extractDocxText } from "@/lib/docx";
import { Job } from "@/lib/types";
import { JobDetail } from "./job-detail";
import Link from "next/link";

export const dynamic = "force-dynamic";

export default async function JobPage({
  params,
}: {
  params: Promise<{ platform: string; id: string }>;
}) {
  const { platform, id } = await params;

  const supabase = getSupabase();
  const { data, error } = await supabase
    .from("job_search_main")
    .select("*")
    .eq("source_platform", decodeURIComponent(platform))
    .eq("id", decodeURIComponent(id))
    .single();

  if (error || !data) {
    return (
      <div className="p-8">
        <Link href="/" className="text-accent hover:underline">
          &larr; Back
        </Link>
        <p className="mt-4 text-red-600">
          Job not found: {error?.message ?? "No data"}
        </p>
      </div>
    );
  }

  const job = data as Job;
  let resumeText = "";
  if (job.tailored_resume) {
    resumeText = await extractDocxText(supabase, job.tailored_resume);
  }

  return <JobDetail job={job} resumeText={resumeText} />;
}

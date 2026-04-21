import { getSupabase } from "@/lib/supabase";
import { Job } from "@/lib/types";
import { JobTable } from "./job-table";

export const dynamic = "force-dynamic";

export default async function Home() {
  const supabase = getSupabase();
  const { data, error } = await supabase
    .from("job_search_main")
    .select(
      "id, source_platform, role_title, company, location, compensation, match, status, date_found, tailored_resume, link"
    )
    .order("date_found", { ascending: false });

  if (error) {
    return (
      <div className="p-8 text-red-600">
        Error loading jobs: {error.message}
      </div>
    );
  }

  const jobs = (data ?? []) as Pick<
    Job,
    | "id"
    | "source_platform"
    | "role_title"
    | "company"
    | "location"
    | "compensation"
    | "match"
    | "status"
    | "date_found"
    | "tailored_resume"
    | "link"
  >[];

  return (
    <main className="flex flex-col flex-1 px-4 py-6 max-w-[1400px] mx-auto w-full">
      <h1 className="text-2xl font-bold mb-4">Job Search</h1>
      <JobTable jobs={jobs} />
    </main>
  );
}

import { NextRequest, NextResponse } from "next/server";

const REPO = "blu3c0ral/job-search";
const WORKFLOW = "tailor_resume.yml";

export async function POST(request: NextRequest) {
  const token = process.env.GITHUB_TOKEN;
  if (!token) {
    return NextResponse.json(
      { error: "GITHUB_TOKEN not configured in .env.local" },
      { status: 500 }
    );
  }

  const { platform, job_id } = await request.json();
  if (!platform || !job_id) {
    return NextResponse.json(
      { error: "Missing platform or job_id" },
      { status: 400 }
    );
  }

  const resp = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { platform, job_id },
      }),
    }
  );

  if (!resp.ok) {
    const body = await resp.text();
    return NextResponse.json(
      { error: `GitHub API error: ${resp.status} ${body}` },
      { status: resp.status }
    );
  }

  // 204 = success, no content
  return NextResponse.json({ ok: true });
}

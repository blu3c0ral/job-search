export interface MatchDetail {
  score: number;
  verdict: string;
  dealbreaker_triggered: boolean;
  where_it_aligns: string;
  where_it_breaks_down: string;
  bottom_line: string;
  comp_risk: string;
  comp_note: string;
  pre_screen: string;
  pre_screen_category: string;
}

export interface TailoringChange {
  original: string;
  tailored: string;
  why?: string;
}

export interface TailoringData {
  changes: TailoringChange[];
  gaps: string[];
}

export interface Job {
  id: string;
  source_platform: string;
  role_title: string;
  company: string;
  location: string;
  compensation: string;
  link: string;
  apply_url: string;
  search_term_match: string;
  date_found: string;
  status: string;
  job_description: string;
  match: string;
  match_detail: MatchDetail | null;
  why_this_company: string | null;
  why_this_role: string | null;
  something_i_built_and_proud_of: string | null;
  tailored_resume: string | null;
  // Can be TailoringChange[] (flat array) or TailoringData ({changes, gaps})
  tailoring_changes: TailoringChange[] | TailoringData | null;
}

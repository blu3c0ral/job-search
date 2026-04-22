"use client";

import { useState, useMemo } from "react";
import { Job } from "@/lib/types";

type TableJob = Pick<
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
>;

const MATCH_COLORS: Record<string, string> = {
  "Excelent Match": "bg-green-100 text-green-800",
  "Good Match": "bg-emerald-100 text-emerald-800",
  Borderline: "bg-yellow-100 text-yellow-800",
  Skip: "bg-orange-100 text-orange-800",
  "Hard No": "bg-red-100 text-red-800",
  "Not Relevant": "bg-gray-100 text-gray-600",
};

const STATUS_OPTIONS = ["New", "Applied", "Interviewing", "Rejected", "Offer", "Skipped"];

type SortKey = "date_found" | "company" | "role_title" | "match";
type SortDir = "asc" | "desc";

const MATCH_ORDER = [
  "Excelent Match",
  "Good Match",
  "Borderline",
  "Skip",
  "Hard No",
  "Not Relevant",
];

export function JobTable({ jobs }: { jobs: TableJob[] }) {
  const [matchFilter, setMatchFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("date_found");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const matchValues = useMemo(
    () => [...new Set(jobs.map((j) => j.match).filter(Boolean))],
    [jobs]
  );
  const statusValues = useMemo(
    () => [...new Set(jobs.map((j) => j.status).filter(Boolean))],
    [jobs]
  );

  const filtered = useMemo(() => {
    let result = jobs;
    if (matchFilter) result = result.filter((j) => j.match === matchFilter);
    if (statusFilter) result = result.filter((j) => j.status === statusFilter);
    if (search) {
      const q = search.toLowerCase();
      result = result.filter(
        (j) =>
          j.company?.toLowerCase().includes(q) ||
          j.role_title?.toLowerCase().includes(q) ||
          j.location?.toLowerCase().includes(q)
      );
    }
    result = [...result].sort((a, b) => {
      let cmp = 0;
      if (sortKey === "match") {
        cmp =
          MATCH_ORDER.indexOf(a.match || "") -
          MATCH_ORDER.indexOf(b.match || "");
      } else {
        const av = (a[sortKey] ?? "") as string;
        const bv = (b[sortKey] ?? "") as string;
        cmp = av.localeCompare(bv);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return result;
  }, [jobs, matchFilter, statusFilter, search, sortKey, sortDir]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "date_found" ? "desc" : "asc");
    }
  }

  function sortIndicator(key: SortKey) {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? " \u25B2" : " \u25BC";
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-center">
        <input
          type="text"
          placeholder="Search company, role, location..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="border border-border rounded-md px-3 py-1.5 text-sm w-64 focus:outline-none focus:ring-2 focus:ring-accent"
        />
        <select
          value={matchFilter}
          onChange={(e) => setMatchFilter(e.target.value)}
          className="border border-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
        >
          <option value="">All matches</option>
          {matchValues.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="border border-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
        >
          <option value="">All statuses</option>
          {statusValues.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
        <span className="text-sm text-gray-500 ml-auto">
          {filtered.length} of {jobs.length} jobs
        </span>
      </div>

      {/* Table */}
      <div className="border border-border rounded-lg overflow-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted text-left">
              <th
                className="px-3 py-2 font-medium cursor-pointer select-none"
                onClick={() => toggleSort("company")}
              >
                Company{sortIndicator("company")}
              </th>
              <th
                className="px-3 py-2 font-medium cursor-pointer select-none"
                onClick={() => toggleSort("role_title")}
              >
                Role{sortIndicator("role_title")}
              </th>
              <th
                className="px-3 py-2 font-medium cursor-pointer select-none"
                onClick={() => toggleSort("match")}
              >
                Match{sortIndicator("match")}
              </th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Location</th>
              <th className="px-3 py-2 font-medium">Comp</th>
              <th
                className="px-3 py-2 font-medium cursor-pointer select-none"
                onClick={() => toggleSort("date_found")}
              >
                Date{sortIndicator("date_found")}
              </th>
              <th className="px-3 py-2 font-medium text-center">Resume</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((job) => (
              <tr
                key={`${job.source_platform}-${job.id}`}
                className="border-b border-border hover:bg-blue-50 cursor-pointer transition-colors"
                onClick={() => {
                  window.location.href = `/job/${encodeURIComponent(job.source_platform)}/${encodeURIComponent(job.id)}`;
                }}
              >
                <td className="px-3 py-2 font-medium">{job.company}</td>
                <td className="px-3 py-2 max-w-[300px] truncate">
                  {job.role_title}
                </td>
                <td className="px-3 py-2">
                  {job.match && (
                    <span
                      className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${MATCH_COLORS[job.match] ?? "bg-gray-100"}`}
                    >
                      {job.match}
                    </span>
                  )}
                </td>
                <td className="px-3 py-2">{job.status}</td>
                <td className="px-3 py-2 max-w-[180px] truncate">
                  {job.location}
                </td>
                <td className="px-3 py-2 max-w-[140px] truncate text-xs">
                  {job.compensation}
                </td>
                <td className="px-3 py-2 whitespace-nowrap">
                  {job.date_found}
                </td>
                <td className="px-3 py-2 text-center">
                  {job.tailored_resume ? "\u2705" : ""}
                </td>
              </tr>
            ))}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-8 text-center text-gray-400">
                  No jobs match your filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

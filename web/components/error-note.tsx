import { ApiError } from "@/lib/api";

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const text =
    error instanceof ApiError
      ? error.status === 403
        ? "You are not the owner or an approver of this dot."
        : `${error.message} (HTTP ${error.status})`
      : "The API could not be reached. Check that it is running.";
  return (
    <p role="alert" className="mt-4 rounded-md border border-bad/30 bg-bad/5 px-3 py-2 text-sm text-bad">
      {text}
    </p>
  );
}

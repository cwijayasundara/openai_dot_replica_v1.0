const format = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function Time({ at }: { at: string | null }) {
  if (!at) return null;
  const date = new Date(at);
  return (
    <time dateTime={at} title={date.toISOString()}>
      {format.format(date)}
    </time>
  );
}

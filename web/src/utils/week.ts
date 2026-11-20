/** 解析 YYYY-MM-DD 开头的日期（本地时区）。 */
export function parseDay(text: string | null | undefined): Date | null {
  const match = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(String(text ?? "").trim());
  if (!match) return null;
  const day = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(day.getTime()) ? null : day;
}

export function monday(day: Date): Date {
  const copy = new Date(day.getFullYear(), day.getMonth(), day.getDate());
  copy.setDate(copy.getDate() - ((copy.getDay() + 6) % 7));
  return copy;
}

/** 教学周：从学期第 1 周所在周一起算。没有起始日时使用解析器给出的周次。 */
export function weekFor(session: { date?: string; week?: number | null }, termStart: string): number | null {
  const start = parseDay(termStart);
  const day = parseDay(session.date);
  if (start && day) {
    return Math.max(1, Math.round((monday(day).getTime() - monday(start).getTime()) / 604800000) + 1);
  }
  return session.week ?? null;
}

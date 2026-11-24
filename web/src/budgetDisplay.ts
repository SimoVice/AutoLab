export function budgetRange(low: number | null, high: number | null, currency = 'USD') {
  if (low === null) return 'None recorded';
  const format = (amount: number) => new Intl.NumberFormat('en-US', {
    style: 'currency', currency, minimumFractionDigits: 0, maximumFractionDigits: 2,
  }).format(amount);
  if (high === null) return `From ${format(low)}`;
  return low === high ? format(low) : `${format(low)}–${format(high)}`;
}

interface Budget {
  currency?: string
  amount?: number | null
}

export function formatBudget(budget?: Budget | null): string {
  const amount = budget?.amount
  if (amount === null || amount === undefined || !Number.isFinite(amount)) {
    return 'Flexible Budget'
  }

  const currency = budget?.currency?.trim().toUpperCase()
  if (!currency) return amount.toLocaleString('en-IN')

  try {
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency,
      currencyDisplay: 'narrowSymbol',
      minimumFractionDigits: 0,
    }).format(amount)
  } catch {
    return `${currency} ${amount.toLocaleString('en-IN')}`
  }
}

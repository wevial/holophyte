/** A mirrored Linear identifier; older mirrors remain plain text. */
export function TicketLink({ ticket, ticket_url }: { ticket: string; ticket_url?: string | null }) {
  if (!ticket_url) return <>{ticket}</>;
  return (
    <a href={ticket_url} target="_blank" rel="noopener noreferrer"
      className="underline-offset-2 hover:underline"
      onClick={(event) => event.stopPropagation()}>
      {ticket}
    </a>
  );
}

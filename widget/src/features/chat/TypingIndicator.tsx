export function TypingIndicator() {
  return (
    <span role="status" aria-label="Assistant is typing" className="flex h-5 items-center gap-1 px-1">
      {[0, 150, 300].map((delay) => (
        <span
          key={delay}
          className="size-1.5 rounded-full bg-zinc-400 motion-safe:animate-typing"
          style={{ animationDelay: `${delay}ms` }}
        />
      ))}
    </span>
  );
}

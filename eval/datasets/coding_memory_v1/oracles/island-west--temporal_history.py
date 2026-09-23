"""Immutable oracle for island-west:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-island-west', service.current_policy()


if __name__ == "__main__":
    main()

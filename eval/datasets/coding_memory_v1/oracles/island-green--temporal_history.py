"""Immutable oracle for island-green:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-island-green', service.current_policy()


if __name__ == "__main__":
    main()

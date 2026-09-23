"""Immutable oracle for grove-west:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-grove-west', service.current_policy()


if __name__ == "__main__":
    main()

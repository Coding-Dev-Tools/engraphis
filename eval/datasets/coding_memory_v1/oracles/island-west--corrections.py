"""Immutable oracle for island-west:corrections."""
import service


def main():
    assert service.current_timeout() == 158, service.current_timeout()


if __name__ == "__main__":
    main()

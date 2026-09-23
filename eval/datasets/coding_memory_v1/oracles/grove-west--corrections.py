"""Immutable oracle for grove-west:corrections."""
import service


def main():
    assert service.current_timeout() == 134, service.current_timeout()


if __name__ == "__main__":
    main()

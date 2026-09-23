"""Immutable oracle for borealis-north:corrections."""
import service


def main():
    assert service.current_timeout() == 70, service.current_timeout()


if __name__ == "__main__":
    main()

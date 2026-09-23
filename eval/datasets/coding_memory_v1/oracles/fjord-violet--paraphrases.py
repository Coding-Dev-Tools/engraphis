"""Immutable oracle for fjord-violet:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-fjord-violet', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
